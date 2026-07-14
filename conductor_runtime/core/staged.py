"""Verified external staging and transactional application."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Dict, Optional

from ..errors import PolicyError, ValidationError
from .policy import RuntimePolicy, require_approval
from .safe import (
    canonical_json_bytes,
    ensure_directory,
    load_json,
    read_regular_bytes,
    reject_symlink_components,
    replace_bytes,
    require_relative,
    resolve_under,
    sha256_bytes,
    write_new_bytes,
    write_new_json,
)
from .state import RunState


STAGE_SCHEMA = "conductor.core_stage.v1"
APPLY_SCHEMA = "conductor.core_stage_apply.v1"
APPLY_APPROVAL = "verified-stage-apply"
DELETE_APPROVAL = "verified-stage-delete"
MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024
MAX_CHANGES = 5000
EXCLUDED_DIRECTORIES = {
    ".codex-conductor",
    ".git",
    ".runtime-state",
    ".venv",
    "__pycache__",
    "node_modules",
}


def create_stage(run_dir: Path, step_id: str, workspace: Path) -> Dict:
    source = Path(workspace).resolve()
    reject_symlink_components(source, "source workspace")
    stage_root = Path(run_dir) / "stages" / step_id
    if stage_root.exists() or stage_root.is_symlink():
        raise ValidationError("stage already exists")
    ensure_directory(stage_root, "stage root")
    stage = stage_root / "workspace"
    os.mkdir(stage, 0o700)
    before = snapshot_workspace(source)
    _copy_snapshot(source, stage, before)
    copied = snapshot_workspace(stage)
    if copied["fingerprint_sha256"] != before["fingerprint_sha256"]:
        raise ValidationError("staged workspace does not match its source snapshot")
    return {
        "stage_dir": stage,
        "before": before,
    }


def finalize_stage(
    *,
    run_dir: Path,
    run_id: str,
    step_id: str,
    workspace: Path,
    stage_dir: Path,
    before: Dict,
    verification_status: str = "pending",
) -> Dict:
    if verification_status not in {"pending", "passed"}:
        raise ValidationError("stage verification status is invalid")
    after = snapshot_workspace(stage_dir)
    changes = workspace_delta(before, after)
    value = {
        "schema": STAGE_SCHEMA,
        "run_id": run_id,
        "step_id": step_id,
        "workspace_sha256": sha256_bytes(str(Path(workspace).resolve()).encode("utf-8")),
        "stage_subdir": "stages/%s/workspace" % step_id,
        "before": before,
        "after": after,
        "changes": changes,
        "change_count": len(changes),
        "verification_status": verification_status,
        "run_status_required": "completed",
    }
    return _with_hash(value, "evidence_sha256")


def mark_stage_verified(evidence: Dict, verifier_step_ids: list) -> Dict:
    _validate_evidence(evidence)
    if (
        not isinstance(verifier_step_ids, list)
        or not verifier_step_ids
        or not all(isinstance(step_id, str) and step_id for step_id in verifier_step_ids)
        or len(verifier_step_ids) != len(set(verifier_step_ids))
    ):
        raise ValidationError("verified stage requires bounded verifier step ids")
    value = dict(evidence)
    value["verification_status"] = "passed"
    value["verified_by_steps"] = list(verifier_step_ids)
    return _with_hash(value, "evidence_sha256")


def snapshot_workspace(root: Path) -> Dict:
    base = Path(root).resolve()
    reject_symlink_components(base, "workspace snapshot")
    files = []
    total_bytes = 0
    for current, directories, filenames in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        clean_directories = []
        for name in sorted(directories):
            candidate = current_path / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValidationError("workspace snapshot rejects symlinked directories")
            if name not in EXCLUDED_DIRECTORIES:
                clean_directories.append(name)
        directories[:] = clean_directories
        for name in sorted(filenames):
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValidationError("workspace snapshot accepts only regular files")
            relative = path.relative_to(base).as_posix()
            require_relative(relative, "workspace file")
            payload = read_regular_bytes(path, "workspace file", MAX_BYTES)
            total_bytes += len(payload)
            if total_bytes > MAX_BYTES:
                raise ValidationError("workspace snapshot exceeds %d bytes" % MAX_BYTES)
            files.append(
                {
                    "path": relative,
                    "sha256": sha256_bytes(payload),
                    "size_bytes": len(payload),
                    "mode": stat.S_IMODE(info.st_mode) & 0o777,
                }
            )
            if len(files) > MAX_FILES:
                raise ValidationError("workspace snapshot exceeds %d files" % MAX_FILES)
    payload = canonical_json_bytes(files)
    return {
        "files": files,
        "file_count": len(files),
        "size_bytes": total_bytes,
        "fingerprint_sha256": sha256_bytes(payload),
        "excluded_directories": sorted(EXCLUDED_DIRECTORIES),
    }


def workspace_delta(before: Dict, after: Dict) -> list:
    _validate_snapshot(before, "before snapshot")
    _validate_snapshot(after, "after snapshot")
    left = {record["path"]: record for record in before["files"]}
    right = {record["path"]: record for record in after["files"]}
    changes = []
    for relative in sorted(set(left) | set(right)):
        if relative not in left:
            action = "added"
        elif relative not in right:
            action = "deleted"
        elif left[relative] == right[relative]:
            continue
        else:
            action = "modified"
        changes.append(
            {
                "path": relative,
                "action": action,
                "before_sha256": left.get(relative, {}).get("sha256"),
                "after_sha256": right.get(relative, {}).get("sha256"),
                "after_mode": right.get(relative, {}).get("mode"),
            }
        )
        if len(changes) > MAX_CHANGES:
            raise ValidationError("staged delta exceeds %d files" % MAX_CHANGES)
    return changes


def apply_verified_stage(
    evidence_path: Path,
    workspace: Path,
    *,
    receipt_path: Optional[Path] = None,
    policy: Optional[RuntimePolicy] = None,
) -> Dict:
    active_policy = policy or RuntimePolicy()
    if not active_policy.allow_writes:
        raise PolicyError("verified stage apply requires --allow-writes")
    require_approval(active_policy, APPLY_APPROVAL, "verified stage apply")
    evidence_file = Path(evidence_path).expanduser()
    evidence = load_json(evidence_file, "stage evidence", 32 * 1024 * 1024)
    _validate_evidence(evidence)
    if evidence["verification_status"] != "passed":
        raise ValidationError("stage evidence is not independently verified")
    run_dir = _run_dir_from_evidence(evidence_file)
    run = RunState.inspect(run_dir)
    if run.state.get("status") != "completed" or run.state.get("run_id") != evidence["run_id"]:
        raise ValidationError("stage evidence does not belong to a completed run")
    source = Path(workspace).resolve()
    if sha256_bytes(str(source).encode("utf-8")) != evidence["workspace_sha256"]:
        raise ValidationError("stage evidence belongs to another workspace")
    stage = resolve_under(run_dir, evidence["stage_subdir"], "stage directory")
    observed_stage = snapshot_workspace(stage)
    if observed_stage["fingerprint_sha256"] != evidence["after"]["fingerprint_sha256"]:
        raise ValidationError("staged workspace changed after verification")
    receipt = Path(receipt_path) if receipt_path is not None else evidence_file.with_name(evidence_file.stem + ".apply.json")
    if receipt.exists() or receipt.is_symlink():
        existing = load_json(receipt, "stage apply receipt")
        _validate_apply_receipt(existing)
        current = snapshot_workspace(source)
        if (
            existing["evidence_sha256"] == evidence["evidence_sha256"]
            and current["fingerprint_sha256"] == evidence["after"]["fingerprint_sha256"]
        ):
            return {
                "status": "merged",
                "change_count": evidence["change_count"],
                "already_applied": True,
                "receipt": existing,
            }
        raise ValidationError("existing stage apply receipt does not match workspace state")
    current = snapshot_workspace(source)
    if current["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
        raise ValidationError("workspace changed after the stage was created")
    destructive = any(change["action"] == "deleted" for change in evidence["changes"])
    if destructive:
        if not active_policy.allow_destructive:
            raise PolicyError("stage deletes files and requires --allow-destructive")
        require_approval(active_policy, DELETE_APPROVAL, "stage deletion")
    backup = Path(run_dir) / "apply-backups" / evidence["step_id"]
    if backup.exists() or backup.is_symlink():
        raise ValidationError("stage apply backup already exists")
    ensure_directory(backup, "stage apply backup")
    _copy_snapshot(source, backup, evidence["before"])
    try:
        _apply_changes(source, stage, evidence["changes"])
        merged = snapshot_workspace(source)
        if merged["fingerprint_sha256"] != evidence["after"]["fingerprint_sha256"]:
            raise ValidationError("applied workspace does not match verified stage")
    except BaseException:
        _restore_snapshot(source, backup, evidence["before"], evidence["after"])
        raise
    value = {
        "schema": APPLY_SCHEMA,
        "status": "merged",
        "run_id": evidence["run_id"],
        "step_id": evidence["step_id"],
        "evidence_sha256": evidence["evidence_sha256"],
        "workspace_sha256": evidence["workspace_sha256"],
        "before_fingerprint_sha256": evidence["before"]["fingerprint_sha256"],
        "after_fingerprint_sha256": evidence["after"]["fingerprint_sha256"],
        "change_count": evidence["change_count"],
        "destructive": destructive,
        "provider_calls": 0,
        "verifier_calls": 0,
        "approval_values_persisted": False,
    }
    value = _with_hash(value, "receipt_sha256")
    write_new_json(receipt, value, "stage apply receipt")
    return {
        "status": "merged",
        "change_count": evidence["change_count"],
        "already_applied": False,
        "receipt": value,
    }


def _copy_snapshot(source: Path, destination: Path, snapshot: Dict) -> None:
    _validate_snapshot(snapshot, "copy snapshot")
    for record in snapshot["files"]:
        source_file = resolve_under(source, record["path"], "source file")
        destination_file = resolve_under(destination, record["path"], "destination file")
        payload = read_regular_bytes(source_file, "source file", MAX_BYTES)
        if sha256_bytes(payload) != record["sha256"]:
            raise ValidationError("source file changed while staging")
        write_new_bytes(destination_file, payload, "destination file", mode=record["mode"])


def _apply_changes(source: Path, stage: Path, changes: list) -> None:
    for change in changes:
        destination = resolve_under(source, change["path"], "apply destination")
        if change["action"] == "deleted":
            reject_symlink_components(destination, "apply destination")
            destination.unlink()
            continue
        staged_file = resolve_under(stage, change["path"], "staged file")
        payload = read_regular_bytes(staged_file, "staged file", MAX_BYTES)
        if sha256_bytes(payload) != change["after_sha256"]:
            raise ValidationError("staged file changed during apply")
        if destination.exists():
            replace_bytes(destination, payload, "apply destination", mode=change["after_mode"])
        else:
            write_new_bytes(destination, payload, "apply destination", mode=change["after_mode"])


def _restore_snapshot(source: Path, backup: Path, before: Dict, after: Dict) -> None:
    before_paths = {record["path"] for record in before["files"]}
    after_paths = {record["path"] for record in after["files"]}
    for relative in sorted(after_paths - before_paths, reverse=True):
        path = resolve_under(source, relative, "rollback path")
        if path.exists() and path.is_file() and not path.is_symlink():
            path.unlink()
    for record in before["files"]:
        backup_file = resolve_under(backup, record["path"], "backup file")
        destination = resolve_under(source, record["path"], "rollback destination")
        payload = read_regular_bytes(backup_file, "backup file", MAX_BYTES)
        if destination.exists():
            replace_bytes(destination, payload, "rollback destination", mode=record["mode"])
        else:
            write_new_bytes(destination, payload, "rollback destination", mode=record["mode"])


def _validate_snapshot(value, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "files",
        "file_count",
        "size_bytes",
        "fingerprint_sha256",
        "excluded_directories",
    }:
        raise ValidationError("%s is invalid" % label)
    files = value["files"]
    if not isinstance(files, list) or len(files) != value["file_count"] or len(files) > MAX_FILES:
        raise ValidationError("%s file list is invalid" % label)
    if value["excluded_directories"] != sorted(EXCLUDED_DIRECTORIES):
        raise ValidationError("%s exclusions changed" % label)
    if sha256_bytes(canonical_json_bytes(files)) != value["fingerprint_sha256"]:
        raise ValidationError("%s fingerprint does not match" % label)


def _validate_evidence(value) -> None:
    if not isinstance(value, dict) or value.get("schema") != STAGE_SCHEMA:
        raise ValidationError("stage evidence schema is invalid")
    observed = value.get("evidence_sha256")
    if observed != _with_hash(value, "evidence_sha256")["evidence_sha256"]:
        raise ValidationError("stage evidence hash does not match")
    _validate_snapshot(value.get("before"), "stage before snapshot")
    _validate_snapshot(value.get("after"), "stage after snapshot")
    if value.get("changes") != workspace_delta(value["before"], value["after"]):
        raise ValidationError("stage evidence delta does not match snapshots")
    if value.get("change_count") != len(value["changes"]):
        raise ValidationError("stage evidence change count does not match")
    verified_by = value.get("verified_by_steps", [])
    if (
        not isinstance(verified_by, list)
        or len(verified_by) > 128
        or not all(isinstance(step_id, str) and step_id for step_id in verified_by)
        or len(verified_by) != len(set(verified_by))
    ):
        raise ValidationError("stage verifier evidence is invalid")
    if value.get("verification_status") == "passed" and not verified_by:
        raise ValidationError("verified stage evidence must identify a verifier step")
    require_relative(value.get("stage_subdir"), "stage subdirectory")


def _validate_apply_receipt(value) -> None:
    if not isinstance(value, dict) or value.get("schema") != APPLY_SCHEMA:
        raise ValidationError("stage apply receipt schema is invalid")
    if value.get("receipt_sha256") != _with_hash(value, "receipt_sha256")["receipt_sha256"]:
        raise ValidationError("stage apply receipt hash does not match")


def _run_dir_from_evidence(evidence_path: Path) -> Path:
    candidate = evidence_path.resolve()
    for parent in candidate.parents:
        if (parent / "run.json").is_file() and (parent / "state.json").is_file():
            return parent
    raise ValidationError("stage evidence is not inside a run directory")


def _with_hash(value: Dict, field: str) -> Dict:
    result = dict(value)
    result.pop(field, None)
    result[field] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result
