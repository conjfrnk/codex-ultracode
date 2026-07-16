"""Verified external staging and transactional application."""

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Dict, Optional, cast

from ..errors import PolicyError, ValidationError
from .policy import RuntimePolicy, require_approval
from .safe import (
    canonical_json_bytes,
    ensure_directory,
    load_json,
    read_regular_bytes,
    reject_symlink_components,
    replace_bytes,
    require_external_state_path,
    require_relative,
    resolve_under,
    sha256_bytes,
    strict_json_bytes,
    write_new_bytes,
    write_new_json,
)
from .state import RunState, workspace_apply_lock


STAGE_SCHEMA = "conductor.core_stage.v1"
PENDING_STAGE_SCHEMA = "conductor.core_pending_stage.v1"
APPLY_SCHEMA = "conductor.core_stage_apply.v1"
APPLY_APPROVAL = "verified-stage-apply"
DELETE_APPROVAL = "verified-stage-delete"
MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024
MAX_CHANGES = 5000
MAX_STAGE_BYTES = MAX_BYTES
MAX_STAGE_ENTRIES = 100000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
EXCLUDED_DIRECTORIES = {
    ".codex-conductor",
    ".git",
    ".runtime-state",
    ".venv",
    "__pycache__",
    "node_modules",
}


def create_stage(run_dir: Path, step_id: str, workspace: Path) -> Dict:
    requested_source = Path(workspace).expanduser()
    reject_symlink_components(requested_source, "source workspace")
    source = requested_source.resolve()
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
    validate_stage_workspace(stage)
    return {
        "stage_dir": stage,
        "before": before,
    }


def discard_stage(run_dir: Path, stage_dir: Path) -> None:
    root = (Path(run_dir) / "stages").resolve()
    stage = Path(stage_dir)
    candidate = stage.parent
    reject_symlink_components(candidate, "discarded stage")
    try:
        candidate.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError("discarded stage is outside its run") from exc
    if stage.name != "workspace" or candidate.parent.resolve() != root:
        raise ValidationError("discarded stage path is invalid")
    shutil.rmtree(candidate)


def pending_stage_descriptor(
    *,
    run_dir: Path,
    run_id: str,
    step_id: str,
    attempt: int,
    workspace: Path,
    stage_dir: Path,
    before: Dict,
) -> Dict:
    """Build immutable metadata that binds a resumable writable stage."""
    _validate_snapshot(before, "pending stage source snapshot")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValidationError("pending stage run id is invalid")
    if not isinstance(step_id, str) or SAFE_ID_RE.fullmatch(step_id) is None:
        raise ValidationError("pending stage step id is invalid")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 1000:
        raise ValidationError("pending stage attempt is invalid")
    requested_source = Path(workspace).expanduser()
    reject_symlink_components(requested_source, "pending stage source workspace")
    source = requested_source.resolve()
    expected_subdir = "stages/%s/workspace" % step_id
    expected_stage = resolve_under(run_dir, expected_subdir, "pending stage directory")
    observed_stage = Path(stage_dir).resolve()
    if observed_stage != expected_stage.resolve():
        raise ValidationError("pending stage path is invalid")
    reject_symlink_components(expected_stage, "pending stage directory")
    value = {
        "schema": PENDING_STAGE_SCHEMA,
        "run_id": run_id,
        "step_id": step_id,
        "attempt": attempt,
        "workspace_sha256": sha256_bytes(str(source).encode("utf-8")),
        "stage_subdir": expected_subdir,
        "stage_path_sha256": sha256_bytes(str(expected_stage.resolve()).encode("utf-8")),
        "before_fingerprint_sha256": before["fingerprint_sha256"],
        "before_file_count": before["file_count"],
        "before_size_bytes": before["size_bytes"],
    }
    return _with_hash(value, "descriptor_sha256")


def load_pending_stage(
    *,
    run: RunState,
    descriptor_relative: str,
    step_id: str,
    workspace: Path,
) -> Dict:
    payload = run.read_artifact(descriptor_relative)
    descriptor = strict_json_bytes(payload, "pending stage descriptor")
    _validate_pending_stage_descriptor(descriptor)
    if descriptor_relative != "stages/%s.attempt-%d.pending.json" % (
        step_id,
        descriptor["attempt"],
    ):
        raise ValidationError("pending stage descriptor path is invalid")
    requested_source = Path(workspace).expanduser()
    reject_symlink_components(requested_source, "pending stage source workspace")
    source = requested_source.resolve()
    if (
        descriptor["run_id"] != run.descriptor["run_id"]
        or descriptor["step_id"] != step_id
        or descriptor["workspace_sha256"] != sha256_bytes(str(source).encode("utf-8"))
        or descriptor["attempt"] > run.state["steps"][step_id]["attempt"]
    ):
        raise ValidationError("pending stage descriptor binding is invalid")
    stage = resolve_under(run.run_dir, descriptor["stage_subdir"], "pending stage directory")
    reject_symlink_components(stage, "pending stage directory")
    if not stage.is_dir():
        raise ValidationError("pending stage directory is missing")
    if descriptor["stage_path_sha256"] != sha256_bytes(str(stage.resolve()).encode("utf-8")):
        raise ValidationError("pending stage canonical path changed")
    source_snapshot = snapshot_workspace(source)
    if (
        source_snapshot["fingerprint_sha256"] != descriptor["before_fingerprint_sha256"]
        or source_snapshot["file_count"] != descriptor["before_file_count"]
        or source_snapshot["size_bytes"] != descriptor["before_size_bytes"]
    ):
        raise ValidationError("source workspace changed while a stage was pending")
    snapshot_workspace(stage)
    return {
        "step_id": step_id,
        "stage_dir": stage,
        "before": source_snapshot,
        "pending_relative": descriptor_relative,
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
    if not isinstance(verification_status, str) or verification_status not in {"pending", "passed"}:
        raise ValidationError("stage verification status is invalid")
    validate_stage_workspace(stage_dir)
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
    validate_stage_evidence(evidence)
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
    requested_root = Path(root).expanduser()
    reject_symlink_components(requested_root, "workspace snapshot")
    base = requested_root.resolve()
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


def validate_stage_workspace(root: Path) -> Dict:
    """Account for the complete stage tree, including normally excluded roots."""
    requested_root = Path(root).expanduser()
    reject_symlink_components(requested_root, "stage quota scan")
    base = requested_root.resolve()
    if not base.is_dir():
        raise ValidationError("stage quota scan root must be a directory")
    entries = 0
    total_bytes = 0
    excluded: list[str] = []
    pending = [base]
    while pending:
        current_path = pending.pop()
        try:
            iterator = os.scandir(current_path)
        except OSError as exc:
            raise ValidationError("cannot scan staged workspace") from exc
        with iterator:
            for entry in iterator:
                path = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ValidationError("cannot inspect staged workspace") from exc
                entries += 1
                if entries > MAX_STAGE_ENTRIES:
                    raise ValidationError("stage exceeds %d total entries" % MAX_STAGE_ENTRIES)
                if stat.S_ISDIR(info.st_mode):
                    relative = path.relative_to(base).as_posix()
                    require_relative(relative, "stage directory")
                    if entry.name in EXCLUDED_DIRECTORIES and len(excluded) < 8:
                        excluded.append(relative)
                    pending.append(path)
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValidationError("stage quota scan accepts only regular files and directories")
                relative = path.relative_to(base).as_posix()
                require_relative(relative, "stage file")
                total_bytes += info.st_size
                if total_bytes > MAX_STAGE_BYTES:
                    raise ValidationError("stage exceeds %d total bytes" % MAX_STAGE_BYTES)
    if excluded:
        raise ValidationError("stage contains excluded directories: %s" % ", ".join(sorted(excluded)))
    return {"entry_count": entries, "size_bytes": total_bytes}


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
    evidence_payload = read_regular_bytes(evidence_file, "stage evidence", 32 * 1024 * 1024)
    evidence = strict_json_bytes(evidence_payload, "stage evidence")
    validate_stage_evidence(evidence)
    if evidence["verification_status"] != "passed":
        raise ValidationError("stage evidence is not independently verified")
    requested_source = Path(workspace).expanduser()
    reject_symlink_components(requested_source, "source workspace")
    source = requested_source.resolve()
    run_dir = require_external_state_path(
        _run_dir_from_evidence(evidence_file), source, "stage run directory"
    )
    run = RunState.inspect(run_dir)
    receipt = Path(receipt_path) if receipt_path is not None else evidence_file.with_name(evidence_file.stem + ".apply.json")
    receipt = require_external_state_path(receipt, source, "stage apply receipt")
    with run.lock():
        with workspace_apply_lock(source):
            run.reload()
            run.verify_recorded_artifacts()
            evidence_relative = _evidence_artifact_relative(evidence_file, run_dir)
            if run.read_artifact(evidence_relative, 32 * 1024 * 1024) != evidence_payload:
                raise ValidationError("stage evidence changed before application")
            return _apply_verified_stage_locked(
                evidence=evidence,
                source=source,
                run=run,
                run_dir=run_dir,
                receipt=receipt,
                policy=active_policy,
            )


def _apply_verified_stage_locked(
    *,
    evidence: Dict,
    source: Path,
    run: RunState,
    run_dir: Path,
    receipt: Path,
    policy: RuntimePolicy,
) -> Dict:
    if run.state.get("status") != "completed" or run.state.get("run_id") != evidence["run_id"]:
        raise ValidationError("stage evidence does not belong to a completed run")
    if sha256_bytes(str(source).encode("utf-8")) != evidence["workspace_sha256"]:
        raise ValidationError("stage evidence belongs to another workspace")
    stage = resolve_under(run_dir, evidence["stage_subdir"], "stage directory")
    validate_stage_workspace(stage)
    observed_stage = snapshot_workspace(stage)
    if observed_stage["fingerprint_sha256"] != evidence["after"]["fingerprint_sha256"]:
        raise ValidationError("staged workspace changed after verification")
    destructive = any(change["action"] == "deleted" for change in evidence["changes"])
    expected_receipt = _apply_receipt(evidence, destructive)
    if receipt.exists() or receipt.is_symlink():
        existing = load_json(receipt, "stage apply receipt")
        _validate_apply_receipt(existing)
        current = snapshot_workspace(source)
        if (
            existing == expected_receipt
            and current["fingerprint_sha256"] == evidence["after"]["fingerprint_sha256"]
        ):
            return {
                "status": "merged",
                "change_count": evidence["change_count"],
                "already_applied": True,
                "receipt": existing,
            }
        raise ValidationError("existing stage apply receipt does not match workspace state")
    if destructive:
        if not policy.allow_destructive:
            raise PolicyError("stage deletes files and requires --allow-destructive")
        require_approval(policy, DELETE_APPROVAL, "stage deletion")
    current = snapshot_workspace(source)
    backup = Path(run_dir) / "apply-backups" / evidence["step_id"]
    if backup.exists() or backup.is_symlink():
        if backup.is_symlink() or not backup.is_dir():
            raise ValidationError("stage apply backup is invalid")
        observed_backup = snapshot_workspace(backup)
        if observed_backup["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
            if current["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
                raise ValidationError("interrupted stage apply has an invalid backup")
            shutil.rmtree(backup)
            backup = _create_apply_backup(source, backup, evidence["before"])
        elif current["fingerprint_sha256"] == evidence["after"]["fingerprint_sha256"]:
            value = _apply_receipt(evidence, destructive)
            write_new_json(receipt, value, "stage apply receipt")
            return _apply_result(evidence, value, already_applied=False)
        elif current["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
            if not _is_interrupted_snapshot(current, evidence["before"], evidence["after"]):
                raise ValidationError("workspace changed during an interrupted stage apply")
            _restore_snapshot(source, backup, evidence["before"], evidence["after"])
            current = snapshot_workspace(source)
            if current["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
                raise ValidationError("interrupted stage apply rollback did not restore the workspace")
    else:
        if current["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
            raise ValidationError("workspace changed after the stage was created")
        backup = _create_apply_backup(source, backup, evidence["before"])
    current = snapshot_workspace(source)
    if current["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
        raise ValidationError("workspace changed before staged application")
    try:
        _apply_changes(source, stage, evidence["changes"])
        merged = snapshot_workspace(source)
        if merged["fingerprint_sha256"] != evidence["after"]["fingerprint_sha256"]:
            raise ValidationError("applied workspace does not match verified stage")
        value = _apply_receipt(evidence, destructive)
        write_new_json(receipt, value, "stage apply receipt")
    except BaseException:
        _restore_snapshot(source, backup, evidence["before"], evidence["after"])
        restored = snapshot_workspace(source)
        if restored["fingerprint_sha256"] != evidence["before"]["fingerprint_sha256"]:
            raise ValidationError("stage apply rollback did not restore the workspace")
        raise
    return _apply_result(evidence, value, already_applied=False)


def _create_apply_backup(source: Path, backup: Path, before: Dict) -> Path:
    ensure_directory(backup, "stage apply backup")
    _copy_snapshot(source, backup, before)
    observed = snapshot_workspace(backup)
    if observed["fingerprint_sha256"] != before["fingerprint_sha256"]:
        raise ValidationError("stage apply backup does not match the source snapshot")
    return backup


def _apply_receipt(evidence: Dict, destructive: bool) -> Dict:
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
    return _with_hash(value, "receipt_sha256")


def _apply_result(evidence: Dict, receipt: Dict, *, already_applied: bool) -> Dict:
    return {
        "status": "merged",
        "change_count": evidence["change_count"],
        "already_applied": already_applied,
        "receipt": receipt,
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


def _is_interrupted_snapshot(current: Dict, before: Dict, after: Dict) -> bool:
    """Accept only pointwise combinations of the bound before/after snapshots."""
    _validate_snapshot(current, "interrupted snapshot")
    left = {record["path"]: record for record in before["files"]}
    right = {record["path"]: record for record in after["files"]}
    observed = {record["path"]: record for record in current["files"]}
    for relative in set(left) | set(right) | set(observed):
        if relative not in left and relative not in right:
            return False
        record = observed.get(relative)
        if record is None:
            if relative in left and relative in right:
                return False
        elif record != left.get(relative) and record != right.get(relative):
            return False
    return True


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
    file_count = value["file_count"]
    size_bytes = value["size_bytes"]
    if (
        not isinstance(files, list)
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or len(files) != file_count
        or len(files) > MAX_FILES
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= MAX_BYTES
    ):
        raise ValidationError("%s file list is invalid" % label)
    if value["excluded_directories"] != sorted(EXCLUDED_DIRECTORIES):
        raise ValidationError("%s exclusions changed" % label)
    paths = set()
    observed_size = 0
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size_bytes", "mode"}:
            raise ValidationError("%s file record is invalid" % label)
        relative = require_relative(cast(str, record.get("path")), "%s file path" % label)
        digest = record.get("sha256")
        size = record.get("size_bytes")
        mode = record.get("mode")
        if (
            relative in paths
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_BYTES
            or isinstance(mode, bool)
            or not isinstance(mode, int)
            or not 0 <= mode <= 0o777
        ):
            raise ValidationError("%s file record is invalid" % label)
        paths.add(relative)
        observed_size += size
    if observed_size != size_bytes:
        raise ValidationError("%s byte count does not match" % label)
    if sha256_bytes(canonical_json_bytes(files)) != value["fingerprint_sha256"]:
        raise ValidationError("%s fingerprint does not match" % label)


def _validate_pending_stage_descriptor(value) -> None:
    fields = {
        "schema",
        "run_id",
        "step_id",
        "attempt",
        "workspace_sha256",
        "stage_subdir",
        "stage_path_sha256",
        "before_fingerprint_sha256",
        "before_file_count",
        "before_size_bytes",
        "descriptor_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema") != PENDING_STAGE_SCHEMA
    ):
        raise ValidationError("pending stage descriptor fields are invalid")
    if value.get("descriptor_sha256") != _with_hash(value, "descriptor_sha256")["descriptor_sha256"]:
        raise ValidationError("pending stage descriptor hash does not match")
    run_id = value.get("run_id")
    step_id = value.get("step_id")
    attempt = value.get("attempt")
    if (
        not isinstance(run_id, str)
        or RUN_ID_RE.fullmatch(run_id) is None
        or not isinstance(step_id, str)
        or SAFE_ID_RE.fullmatch(step_id) is None
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or not 1 <= attempt <= 1000
        or value.get("stage_subdir") != "stages/%s/workspace" % step_id
        or any(
            not isinstance(value.get(field), str) or SHA256_RE.fullmatch(value[field]) is None
            for field in (
                "workspace_sha256",
                "stage_path_sha256",
                "before_fingerprint_sha256",
            )
        )
        or isinstance(value.get("before_file_count"), bool)
        or not isinstance(value.get("before_file_count"), int)
        or not 0 <= value["before_file_count"] <= MAX_FILES
        or isinstance(value.get("before_size_bytes"), bool)
        or not isinstance(value.get("before_size_bytes"), int)
        or not 0 <= value["before_size_bytes"] <= MAX_BYTES
    ):
        raise ValidationError("pending stage descriptor bindings are invalid")


def validate_stage_evidence(value) -> None:
    if not isinstance(value, dict) or value.get("schema") != STAGE_SCHEMA:
        raise ValidationError("stage evidence schema is invalid")
    status = value.get("verification_status")
    fields = {
        "schema",
        "run_id",
        "step_id",
        "workspace_sha256",
        "stage_subdir",
        "before",
        "after",
        "changes",
        "change_count",
        "verification_status",
        "run_status_required",
        "evidence_sha256",
    }
    if status == "passed":
        fields.add("verified_by_steps")
    if set(value) != fields or not isinstance(status, str) or status not in {"pending", "passed"}:
        raise ValidationError("stage evidence fields are invalid")
    observed = value.get("evidence_sha256")
    if not isinstance(observed, str) or observed != _with_hash(value, "evidence_sha256")["evidence_sha256"]:
        raise ValidationError("stage evidence hash does not match")
    run_id = value.get("run_id")
    step_id = value.get("step_id")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValidationError("stage evidence run id is invalid")
    if not isinstance(step_id, str) or SAFE_ID_RE.fullmatch(step_id) is None:
        raise ValidationError("stage evidence step id is invalid")
    if (
        not isinstance(value.get("workspace_sha256"), str)
        or SHA256_RE.fullmatch(value["workspace_sha256"]) is None
        or value.get("stage_subdir") != "stages/%s/workspace" % step_id
        or value.get("run_status_required") != "completed"
    ):
        raise ValidationError("stage evidence bindings are invalid")
    _validate_snapshot(value.get("before"), "stage before snapshot")
    _validate_snapshot(value.get("after"), "stage after snapshot")
    changes = value.get("changes")
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise ValidationError("stage evidence changes are invalid")
    for change in changes:
        if not isinstance(change, dict) or set(change) != {
            "path",
            "action",
            "before_sha256",
            "after_sha256",
            "after_mode",
        }:
            raise ValidationError("stage evidence change record is invalid")
        require_relative(cast(str, change.get("path")), "stage change path")
        action = change.get("action")
        if not isinstance(action, str) or action not in {"added", "modified", "deleted"}:
            raise ValidationError("stage evidence change action is invalid")
    if value.get("changes") != workspace_delta(value["before"], value["after"]):
        raise ValidationError("stage evidence delta does not match snapshots")
    if (
        isinstance(value.get("change_count"), bool)
        or not isinstance(value.get("change_count"), int)
        or value.get("change_count") != len(value["changes"])
    ):
        raise ValidationError("stage evidence change count does not match")
    verified_by = value.get("verified_by_steps", [])
    if (
        not isinstance(verified_by, list)
        or len(verified_by) > 128
        or not all(
            isinstance(step_id, str) and SAFE_ID_RE.fullmatch(step_id) is not None
            for step_id in verified_by
        )
        or len(verified_by) != len(set(verified_by))
    ):
        raise ValidationError("stage verifier evidence is invalid")
    if status == "passed" and not verified_by:
        raise ValidationError("verified stage evidence must identify a verifier step")


def _validate_apply_receipt(value) -> None:
    fields = {
        "schema",
        "status",
        "run_id",
        "step_id",
        "evidence_sha256",
        "workspace_sha256",
        "before_fingerprint_sha256",
        "after_fingerprint_sha256",
        "change_count",
        "destructive",
        "provider_calls",
        "verifier_calls",
        "approval_values_persisted",
        "receipt_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != APPLY_SCHEMA:
        raise ValidationError("stage apply receipt schema is invalid")
    if value.get("receipt_sha256") != _with_hash(value, "receipt_sha256")["receipt_sha256"]:
        raise ValidationError("stage apply receipt hash does not match")
    if (
        value.get("status") != "merged"
        or not isinstance(value.get("run_id"), str)
        or RUN_ID_RE.fullmatch(value["run_id"]) is None
        or not isinstance(value.get("step_id"), str)
        or SAFE_ID_RE.fullmatch(value["step_id"]) is None
        or any(
            not isinstance(value.get(field), str) or SHA256_RE.fullmatch(value[field]) is None
            for field in (
                "evidence_sha256",
                "workspace_sha256",
                "before_fingerprint_sha256",
                "after_fingerprint_sha256",
            )
        )
        or isinstance(value.get("change_count"), bool)
        or not isinstance(value.get("change_count"), int)
        or not 0 <= value["change_count"] <= MAX_CHANGES
        or not isinstance(value.get("destructive"), bool)
        or value.get("provider_calls") != 0
        or value.get("verifier_calls") != 0
        or value.get("approval_values_persisted") is not False
    ):
        raise ValidationError("stage apply receipt fields are invalid")


def _run_dir_from_evidence(evidence_path: Path) -> Path:
    candidate = evidence_path.resolve()
    for parent in candidate.parents:
        if (parent / "run.json").is_file() and (parent / "state.json").is_file():
            return parent
    raise ValidationError("stage evidence is not inside a run directory")


def _evidence_artifact_relative(evidence_path: Path, run_dir: Path) -> str:
    try:
        relative = evidence_path.resolve().relative_to((Path(run_dir) / "artifacts").resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError("stage evidence is not a recorded run artifact") from exc
    return require_relative(relative.as_posix(), "stage evidence artifact")


def _with_hash(value: Dict, field: str) -> Dict:
    result = dict(value)
    result.pop(field, None)
    result[field] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result
