import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Conductor's supported runtime is POSIX.
    fcntl = None

from .artifacts import utc_now
from .benchmark import (
    BENCHMARK_REPORT_SCHEMA,
    MAX_BENCHMARK_JSON_BYTES,
    validate_benchmark_report,
)
from .claude_staged import VERIFIER_RUNTIME_DIRECTORY_NAMES
from .codex_staged_repair import (
    CODEX_STAGED_REPAIR_SCHEMA,
    validate_codex_staged_repair_evidence,
)
from .errors import PolicyError, ValidationError
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    read_regular_file_bytes_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
    write_new_text_file_no_follow,
)
from .staged_delivery import (
    STAGED_DELIVERY_PROVIDER_STATUSES,
    verified_repair_stage_delivery,
    verified_stage_delivery,
)
from .staged_workspace import (
    MAX_STAGED_CHANGES,
    MAX_STAGED_FILES,
    MAX_STAGED_PATCH_BYTES,
    apply_workspace_delta_merge,
    build_workspace_patch,
    plan_workspace_delta_merge,
    reconcile_workspace_delta_merge,
    require_path_outside_workspace,
    snapshot_workspace,
    validate_workspace_merge_plan,
    workspace_snapshot_from_manifest,
    workspace_snapshot_manifest,
)


STAGED_APPLY_SCHEMA = "conductor.staged_apply.v1"
STAGED_APPLY_APPROVAL = "verified-stage-apply"
STAGED_APPLY_DELETE_APPROVAL = "verified-stage-delete"
STAGED_APPLY_STATUSES = {"prepared", "merging", "merged"}
STAGED_APPLY_EVIDENCE_KINDS = {"benchmark-report", "codex-staged-repair"}
STAGED_APPLY_FIELDS = {
    "schema",
    "status",
    "evidence_kind",
    "evidence_schema",
    "evidence_path_sha256",
    "evidence_sha256",
    "workspace_path_sha256",
    "stage_path_sha256",
    "stage_directory_name",
    "patch_path_sha256",
    "patch_name",
    "patch_sha256",
    "patch_bytes",
    "provider_status",
    "staged_status",
    "verification_status",
    "change_count",
    "added",
    "modified",
    "deleted",
    "destructive",
    "policy",
    "source_before_manifest",
    "source_before_fingerprint_sha256",
    "source_excluded_directories",
    "stage_manifest",
    "stage_fingerprint_sha256",
    "stage_excluded_directories",
    "merge_plan",
    "source_after_fingerprint_sha256",
    "receipt_path_sha256",
    "created_at_utc",
    "updated_at_utc",
    "receipt_sha256",
}
STAGED_APPLY_POLICY_FIELDS = {
    "automatic_apply",
    "explicit_apply",
    "approval_values_persisted",
    "provider_calls",
    "verifier_calls",
}
MAX_STAGED_APPLY_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_STAGED_APPLY_NAME_CHARS = 255
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def default_staged_apply_receipt_path(evidence_path: Path) -> Path:
    path = Path(evidence_path)
    return path.with_name(path.stem + ".apply.json")


def preflight_new_verified_stage_apply(
    evidence_path: Path,
    workspace: Path,
    *,
    stage_dir: Optional[Path] = None,
    patch_path: Optional[Path] = None,
    receipt_path: Optional[Path] = None,
    policy: Optional[RuntimePolicy] = None,
) -> Path:
    """Validate a fresh explicit apply request before provider work begins."""
    evidence_input = Path(evidence_path)
    workspace_input = Path(workspace)
    receipt_input = (
        Path(receipt_path)
        if receipt_path is not None
        else default_staged_apply_receipt_path(evidence_input)
    )
    if (stage_dir is None) != (patch_path is None):
        raise ValidationError("verified stage apply preflight requires both stage and patch paths")

    _validate_receipt_location(workspace_input, receipt_input)
    _require_apply_policy(policy or RuntimePolicy(), destructive=False)
    if stage_dir is not None and patch_path is not None:
        _validate_initial_paths(
            workspace_input,
            evidence_input,
            Path(stage_dir),
            Path(patch_path),
            receipt_input,
        )
    else:
        reject_symlink_path(workspace_input, "verified stage apply workspace")
        reject_symlink_path(evidence_input, "verified stage evidence")
        require_path_outside_workspace(
            workspace_input,
            evidence_input,
            "verified stage evidence",
        )
        canonical = {
            _canonical_path(workspace_input),
            _canonical_path(evidence_input),
            _canonical_path(receipt_input),
            _canonical_path(receipt_input.with_name(receipt_input.name + ".lock")),
        }
        if len(canonical) != 4:
            raise ValidationError("verified stage apply paths must be distinct")
    if receipt_input.exists() or receipt_input.is_symlink():
        raise ValidationError(
            "verified stage apply receipt already exists; use apply-verified-stage to resume or verify it"
        )
    return receipt_input


def apply_verified_stage(
    evidence_path: Path,
    workspace: Path,
    *,
    stage_dir: Optional[Path] = None,
    patch_path: Optional[Path] = None,
    receipt_path: Optional[Path] = None,
    policy: Optional[RuntimePolicy] = None,
) -> Dict:
    evidence_input = Path(evidence_path)
    workspace_input = Path(workspace)
    receipt_input = (
        Path(receipt_path)
        if receipt_path is not None
        else default_staged_apply_receipt_path(evidence_input)
    )
    active_policy = policy or RuntimePolicy()

    _validate_receipt_location(workspace_input, receipt_input)
    if receipt_input.exists() or receipt_input.is_symlink():
        observed = load_staged_apply_receipt(receipt_input)
        if observed["status"] == "merged":
            return _verify_merged_receipt(
                observed,
                evidence_input,
                workspace_input,
                stage_dir=stage_dir,
                patch_path=patch_path,
                receipt_path=receipt_input,
            )

    _require_apply_policy(active_policy, destructive=False)
    with _staged_apply_lock(receipt_input):
        if receipt_input.exists() or receipt_input.is_symlink():
            receipt = load_staged_apply_receipt(receipt_input)
            if receipt["status"] == "merged":
                return _verify_merged_receipt(
                    receipt,
                    evidence_input,
                    workspace_input,
                    stage_dir=stage_dir,
                    patch_path=patch_path,
                    receipt_path=receipt_input,
                )
        else:
            context = _inspect_verified_delivery(
                evidence_input,
                workspace_input,
                stage_dir=stage_dir,
                patch_path=patch_path,
                receipt_path=receipt_input,
            )
            _require_apply_policy(active_policy, destructive=context["destructive"])
            receipt = _build_staged_apply_receipt(context, receipt_input)
            _write_new_staged_apply_receipt(receipt_input, receipt)

        _require_apply_policy(active_policy, destructive=receipt["destructive"])
        return _continue_staged_apply(
            receipt,
            evidence_input,
            workspace_input,
            stage_dir=stage_dir,
            patch_path=patch_path,
            receipt_path=receipt_input,
        )


def load_staged_apply_receipt(path: Path) -> Dict:
    receipt_path = Path(path)
    try:
        receipt = json.loads(
            read_regular_text_file_no_follow(
                receipt_path,
                "verified stage apply receipt",
                MAX_STAGED_APPLY_RECEIPT_BYTES,
            ),
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ValidationError(
            "verified stage apply receipt is invalid JSON: %s"
            % exc.__class__.__name__
        )
    validate_staged_apply_receipt(receipt)
    if receipt["receipt_path_sha256"] != _path_sha256(receipt_path):
        raise ValidationError("verified stage apply receipt path binding changed")
    return receipt


def validate_staged_apply_receipt(receipt: Dict) -> None:
    if not isinstance(receipt, dict) or set(receipt) != STAGED_APPLY_FIELDS:
        raise ValidationError("verified stage apply receipt has invalid fields")
    if receipt.get("schema") != STAGED_APPLY_SCHEMA:
        raise ValidationError("verified stage apply receipt schema is invalid")
    status = receipt.get("status")
    if status not in STAGED_APPLY_STATUSES:
        raise ValidationError("verified stage apply receipt status is invalid")
    evidence_kind = receipt.get("evidence_kind")
    if evidence_kind not in STAGED_APPLY_EVIDENCE_KINDS:
        raise ValidationError("verified stage apply evidence kind is invalid")
    expected_schema = (
        BENCHMARK_REPORT_SCHEMA
        if evidence_kind == "benchmark-report"
        else CODEX_STAGED_REPAIR_SCHEMA
    )
    if receipt.get("evidence_schema") != expected_schema:
        raise ValidationError("verified stage apply evidence schema is inconsistent")
    for field in (
        "evidence_path_sha256",
        "evidence_sha256",
        "workspace_path_sha256",
        "stage_path_sha256",
        "patch_path_sha256",
        "patch_sha256",
        "source_before_fingerprint_sha256",
        "stage_fingerprint_sha256",
        "receipt_path_sha256",
        "receipt_sha256",
    ):
        _validate_sha256(receipt.get(field), "verified stage apply %s" % field)
    for field in ("stage_directory_name", "patch_name"):
        _validate_artifact_name(receipt.get(field), "verified stage apply %s" % field)
    for field in ("patch_bytes", "change_count"):
        value = receipt.get(field)
        maximum = MAX_STAGED_PATCH_BYTES if field == "patch_bytes" else MAX_STAGED_CHANGES
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= maximum
        ):
            raise ValidationError("verified stage apply %s is invalid" % field)
    if receipt.get("provider_status") not in STAGED_DELIVERY_PROVIDER_STATUSES:
        raise ValidationError("verified stage apply provider status is invalid")
    if receipt.get("staged_status") != "success":
        raise ValidationError("verified stage apply staged status is invalid")
    if receipt.get("verification_status") != "passed":
        raise ValidationError("verified stage apply verification status is invalid")

    changed = {}
    for field in ("added", "modified", "deleted"):
        changed[field] = _validate_relative_path_list(
            receipt.get(field),
            "verified stage apply %s" % field,
        )
    if any(set(changed[left]) & set(changed[right]) for left, right in (
        ("added", "modified"),
        ("added", "deleted"),
        ("modified", "deleted"),
    )):
        raise ValidationError("verified stage apply change classes overlap")
    all_changed = sorted(changed["added"] + changed["modified"] + changed["deleted"])
    if receipt["change_count"] != len(all_changed):
        raise ValidationError("verified stage apply change count is inconsistent")
    if receipt.get("destructive") is not bool(changed["deleted"]):
        raise ValidationError("verified stage apply destructive summary is inconsistent")

    expected_policy = {
        "automatic_apply": False,
        "explicit_apply": True,
        "approval_values_persisted": False,
        "provider_calls": 0,
        "verifier_calls": 0,
    }
    if receipt.get("policy") != expected_policy or set(receipt["policy"]) != STAGED_APPLY_POLICY_FIELDS:
        raise ValidationError("verified stage apply policy is invalid")

    source_before = workspace_snapshot_from_manifest(receipt.get("source_before_manifest"))
    stage = workspace_snapshot_from_manifest(receipt.get("stage_manifest"))
    source_excluded = _validate_relative_path_list(
        receipt.get("source_excluded_directories"),
        "verified stage apply source excluded directories",
    )
    stage_excluded = _validate_relative_path_list(
        receipt.get("stage_excluded_directories"),
        "verified stage apply stage excluded directories",
    )
    if not source_excluded and receipt["source_before_fingerprint_sha256"] != source_before.fingerprint_sha256:
        raise ValidationError("verified stage apply source fingerprint is inconsistent")
    if not stage_excluded and receipt["stage_fingerprint_sha256"] != stage.fingerprint_sha256:
        raise ValidationError("verified stage apply stage fingerprint is inconsistent")

    plan = receipt.get("merge_plan")
    validate_workspace_merge_plan(
        plan,
        base_snapshot=source_before,
        incoming_snapshot=stage,
    )
    if (
        plan["status"] != "applied"
        or plan["source_before_sha256"] != source_before.tracked_fingerprint_sha256
        or plan["apply_files"] != all_changed
        or plan["changed_files"] != all_changed
        or plan["deduplicated_files"]
        or plan["conflicting_files"]
    ):
        raise ValidationError("verified stage apply merge plan is inconsistent")
    operation_paths = {
        "add": changed["added"],
        "modify": changed["modified"],
        "delete": changed["deleted"],
    }
    for operation, paths in operation_paths.items():
        observed = [
            record["path"]
            for record in plan["records"]
            if record["operation"] == operation
        ]
        if observed != paths:
            raise ValidationError("verified stage apply merge operations changed")

    source_after = receipt.get("source_after_fingerprint_sha256")
    if status == "merged":
        _validate_sha256(source_after, "verified stage apply source after fingerprint")
    elif source_after is not None:
        raise ValidationError("unfinished verified stage apply receipt contains a final fingerprint")
    for field in ("created_at_utc", "updated_at_utc"):
        value = receipt.get(field)
        if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
            raise ValidationError("verified stage apply %s is invalid" % field)
    if receipt["updated_at_utc"] < receipt["created_at_utc"]:
        raise ValidationError("verified stage apply timestamps are inconsistent")
    if receipt["receipt_sha256"] != staged_apply_receipt_sha256(receipt):
        raise ValidationError("verified stage apply receipt hash changed")


def staged_apply_receipt_sha256(receipt: Dict) -> str:
    if not isinstance(receipt, dict):
        raise ValidationError("verified stage apply receipt must be an object")
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _sha256_json(payload)


def _inspect_verified_delivery(
    evidence_path: Path,
    workspace: Path,
    *,
    stage_dir: Optional[Path],
    patch_path: Optional[Path],
    receipt_path: Path,
) -> Dict:
    evidence_raw = read_regular_file_bytes_no_follow(
        evidence_path,
        "verified stage evidence",
        max_bytes=MAX_BENCHMARK_JSON_BYTES,
    )
    evidence = _parse_strict_json(evidence_raw, "verified stage evidence")
    schema = evidence.get("schema") if isinstance(evidence, dict) else None
    if schema == BENCHMARK_REPORT_SCHEMA:
        validate_benchmark_report(evidence, source=str(evidence_path))
        results = evidence.get("results")
        if not isinstance(results, list) or len(results) != 1:
            raise ValidationError("verified stage benchmark report must contain exactly one result")
        result = results[0]
        staged = result.get("staged_evidence")
        if not isinstance(staged, dict):
            raise ValidationError("verified stage benchmark result is missing staged evidence")
        changes = staged["changes"]
        stage_record = staged["stage"]
        source_record = staged["source"]
        resolved_stage = Path(stage_dir) if stage_dir is not None else evidence_path.parent / stage_record["directory_name"]
        resolved_patch = Path(patch_path) if patch_path is not None else evidence_path.parent / changes["patch_name"]
        if not verified_stage_delivery(result, resolved_patch):
            raise ValidationError("benchmark report does not contain an accepted verified stage delivery")
        evidence_kind = "benchmark-report"
        provider_status = result["provider_evidence"]["status"]
        staged_status = staged["status"]
        verification_status = staged["verification"]["status"]
        expected_stage_full = None
        expected_stage_tracked = stage_record["after_verification_sha256"]
    elif schema == CODEX_STAGED_REPAIR_SCHEMA:
        if Path(evidence_path).name != "repair.json":
            raise ValidationError("Codex staged repair apply evidence must be named repair.json")
        validate_codex_staged_repair_evidence(
            evidence,
            source=str(evidence_path),
            base_dir=Path(evidence_path).parent,
        )
        if not verified_repair_stage_delivery(evidence, Path(evidence_path).parent):
            raise ValidationError("repair evidence does not contain an accepted verified stage delivery")
        final = evidence["final"]
        last_attempt = evidence["attempts"][-1]
        changes = final
        source_record = evidence["source"]
        stage_record = {
            "directory_name": final["stage_directory_name"],
            "path_sha256": final["stage_path_sha256"],
            "file_count": None,
            "total_bytes": None,
        }
        resolved_stage = Path(stage_dir) if stage_dir is not None else evidence_path.parent / final["stage_directory_name"]
        resolved_patch = Path(patch_path) if patch_path is not None else evidence_path.parent / final["patch_name"]
        evidence_kind = "codex-staged-repair"
        provider_status = last_attempt["provider_status"]
        staged_status = last_attempt["staged_status"]
        verification_status = final["verification_status"]
        expected_stage_full = final["stage_fingerprint_sha256"]
        expected_stage_tracked = last_attempt["stage_tracked_fingerprint_sha256"]
    else:
        raise ValidationError("verified stage apply requires a benchmark report or Codex repair evidence")

    _validate_initial_paths(
        workspace,
        evidence_path,
        resolved_stage,
        resolved_patch,
        receipt_path,
    )
    if Path(resolved_stage).name != stage_record["directory_name"]:
        raise ValidationError("verified stage directory name binding changed")
    if _path_sha256(resolved_stage) != stage_record["path_sha256"]:
        raise ValidationError("verified stage path binding changed")
    if Path(resolved_patch).name != changes["patch_name"]:
        raise ValidationError("verified stage patch name binding changed")

    source_snapshot = snapshot_workspace(workspace)
    if source_snapshot.fingerprint_sha256 != source_record["before_fingerprint_sha256"]:
        raise ValidationError("source workspace changed after verified stage creation")
    if (
        source_snapshot.file_count != source_record["file_count"]
        or source_snapshot.total_bytes != source_record["total_bytes"]
    ):
        raise ValidationError("source workspace summary changed after verified stage creation")
    stage_snapshot = snapshot_workspace(
        resolved_stage,
        extra_excluded_directory_names=VERIFIER_RUNTIME_DIRECTORY_NAMES,
    )
    if stage_snapshot.tracked_fingerprint_sha256 != expected_stage_tracked:
        raise ValidationError("verified stage tracked fingerprint changed")
    if expected_stage_full is not None and stage_snapshot.fingerprint_sha256 != expected_stage_full:
        raise ValidationError("verified repair stage fingerprint changed")
    if stage_record["file_count"] is not None and (
        stage_snapshot.file_count != stage_record["file_count"]
        or stage_snapshot.total_bytes != stage_record["total_bytes"]
    ):
        raise ValidationError("verified stage summary changed")

    recomputed = build_workspace_patch(
        workspace,
        source_snapshot,
        resolved_stage,
        stage_snapshot,
    )
    for field in (
        "change_count",
        "added",
        "modified",
        "deleted",
        "binary",
        "mode_changed",
        "unpatchable",
        "patch_sha256",
        "patch_bytes",
    ):
        if recomputed[field] != changes[field]:
            raise ValidationError("verified stage %s binding changed" % field)
    if recomputed["binary"] or recomputed["mode_changed"] or recomputed["unpatchable"]:
        raise ValidationError("verified stage contains unsupported file metadata")
    expected_patch = recomputed["patch_text"].encode("utf-8")
    retained_patch = read_regular_file_bytes_no_follow(
        resolved_patch,
        "verified stage patch",
        max_bytes=recomputed["patch_bytes"],
    )
    if retained_patch != expected_patch:
        raise ValidationError("verified stage patch content changed")

    source_final = snapshot_workspace(workspace)
    stage_final = snapshot_workspace(
        resolved_stage,
        extra_excluded_directory_names=VERIFIER_RUNTIME_DIRECTORY_NAMES,
    )
    if source_final != source_snapshot:
        raise ValidationError("source workspace changed while preparing verified stage apply")
    if stage_final != stage_snapshot:
        raise ValidationError("verified stage changed while preparing apply")
    if read_regular_file_bytes_no_follow(
        evidence_path,
        "verified stage evidence",
        max_bytes=MAX_BENCHMARK_JSON_BYTES,
    ) != evidence_raw:
        raise ValidationError("verified stage evidence changed while preparing apply")
    if read_regular_file_bytes_no_follow(
        resolved_patch,
        "verified stage patch",
        max_bytes=recomputed["patch_bytes"],
    ) != retained_patch:
        raise ValidationError("verified stage patch changed while preparing apply")

    plan = plan_workspace_delta_merge(source_snapshot, source_snapshot, stage_snapshot)
    if plan["status"] != "applied" or plan["apply_files"] != plan["changed_files"]:
        raise ValidationError("verified stage did not produce a direct deterministic apply plan")
    return {
        "evidence_kind": evidence_kind,
        "evidence_schema": schema,
        "evidence_path_sha256": _path_sha256(evidence_path),
        "evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
        "workspace_path_sha256": _path_sha256(workspace),
        "stage_path_sha256": _path_sha256(resolved_stage),
        "stage_directory_name": Path(resolved_stage).name,
        "patch_path_sha256": _path_sha256(resolved_patch),
        "patch_name": Path(resolved_patch).name,
        "patch_sha256": recomputed["patch_sha256"],
        "patch_bytes": recomputed["patch_bytes"],
        "provider_status": provider_status,
        "staged_status": staged_status,
        "verification_status": verification_status,
        "change_count": recomputed["change_count"],
        "added": recomputed["added"],
        "modified": recomputed["modified"],
        "deleted": recomputed["deleted"],
        "destructive": bool(recomputed["deleted"]),
        "source_snapshot": source_snapshot,
        "stage_snapshot": stage_snapshot,
        "merge_plan": plan,
    }


def _build_staged_apply_receipt(context: Dict, receipt_path: Path) -> Dict:
    timestamp = utc_now()
    receipt = {
        "schema": STAGED_APPLY_SCHEMA,
        "status": "prepared",
        "evidence_kind": context["evidence_kind"],
        "evidence_schema": context["evidence_schema"],
        "evidence_path_sha256": context["evidence_path_sha256"],
        "evidence_sha256": context["evidence_sha256"],
        "workspace_path_sha256": context["workspace_path_sha256"],
        "stage_path_sha256": context["stage_path_sha256"],
        "stage_directory_name": context["stage_directory_name"],
        "patch_path_sha256": context["patch_path_sha256"],
        "patch_name": context["patch_name"],
        "patch_sha256": context["patch_sha256"],
        "patch_bytes": context["patch_bytes"],
        "provider_status": context["provider_status"],
        "staged_status": context["staged_status"],
        "verification_status": context["verification_status"],
        "change_count": context["change_count"],
        "added": list(context["added"]),
        "modified": list(context["modified"]),
        "deleted": list(context["deleted"]),
        "destructive": context["destructive"],
        "policy": {
            "automatic_apply": False,
            "explicit_apply": True,
            "approval_values_persisted": False,
            "provider_calls": 0,
            "verifier_calls": 0,
        },
        "source_before_manifest": workspace_snapshot_manifest(context["source_snapshot"]),
        "source_before_fingerprint_sha256": context["source_snapshot"].fingerprint_sha256,
        "source_excluded_directories": list(context["source_snapshot"].excluded_directories),
        "stage_manifest": workspace_snapshot_manifest(context["stage_snapshot"]),
        "stage_fingerprint_sha256": context["stage_snapshot"].fingerprint_sha256,
        "stage_excluded_directories": list(context["stage_snapshot"].excluded_directories),
        "merge_plan": json.loads(json.dumps(context["merge_plan"])),
        "source_after_fingerprint_sha256": None,
        "receipt_path_sha256": _path_sha256(receipt_path),
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "receipt_sha256": "0" * 64,
    }
    return _finalize_receipt(receipt)


def _continue_staged_apply(
    receipt: Dict,
    evidence_path: Path,
    workspace: Path,
    *,
    stage_dir: Optional[Path],
    patch_path: Optional[Path],
    receipt_path: Path,
) -> Dict:
    stage, _ = _bind_receipt_paths(
        receipt,
        evidence_path,
        workspace,
        stage_dir=stage_dir,
        patch_path=patch_path,
        receipt_path=receipt_path,
    )
    source_before = workspace_snapshot_from_manifest(receipt["source_before_manifest"])
    incoming = snapshot_workspace(
        stage,
        extra_excluded_directory_names=VERIFIER_RUNTIME_DIRECTORY_NAMES,
    )
    _require_snapshot_binding(
        incoming,
        receipt["stage_manifest"],
        receipt["stage_fingerprint_sha256"],
        receipt["stage_excluded_directories"],
        "verified stage",
    )
    current = snapshot_workspace(workspace)
    if tuple(receipt["source_excluded_directories"]) != current.excluded_directories:
        raise ValidationError("source excluded-directory set changed during verified stage apply")

    if receipt["status"] == "prepared":
        _require_snapshot_binding(
            current,
            receipt["source_before_manifest"],
            receipt["source_before_fingerprint_sha256"],
            receipt["source_excluded_directories"],
            "source workspace",
        )
        receipt = _transition_receipt(receipt, "merging")
        _replace_staged_apply_receipt(receipt_path, receipt)
        result = apply_workspace_delta_merge(
            workspace,
            stage,
            incoming,
            receipt["merge_plan"],
        )
    elif receipt["status"] == "merging":
        result = reconcile_workspace_delta_merge(
            workspace,
            stage,
            incoming,
            receipt["merge_plan"],
            source_before,
        )
    else:
        return _verify_merged_receipt(
            receipt,
            evidence_path,
            workspace,
            stage_dir=stage_dir,
            patch_path=patch_path,
            receipt_path=receipt_path,
        )

    if result.tracked_fingerprint_sha256 != receipt["merge_plan"]["source_after_sha256"]:
        raise ValidationError("verified stage apply result fingerprint is inconsistent")
    if result.excluded_directories != tuple(receipt["source_excluded_directories"]):
        raise ValidationError("source excluded-directory set changed during verified stage merge")
    merged = _transition_receipt(
        receipt,
        "merged",
        source_after_fingerprint_sha256=result.fingerprint_sha256,
    )
    _replace_staged_apply_receipt(receipt_path, merged)
    return _result_summary(merged, already_applied=False)


def _verify_merged_receipt(
    receipt: Dict,
    evidence_path: Path,
    workspace: Path,
    *,
    stage_dir: Optional[Path],
    patch_path: Optional[Path],
    receipt_path: Path,
) -> Dict:
    _bind_receipt_paths(
        receipt,
        evidence_path,
        workspace,
        stage_dir=stage_dir,
        patch_path=patch_path,
        receipt_path=receipt_path,
    )
    current = snapshot_workspace(workspace)
    if (
        current.tracked_fingerprint_sha256 != receipt["merge_plan"]["source_after_sha256"]
        or current.fingerprint_sha256 != receipt["source_after_fingerprint_sha256"]
        or current.excluded_directories != tuple(receipt["source_excluded_directories"])
    ):
        raise ValidationError("workspace changed after verified stage apply completed")
    return _result_summary(receipt, already_applied=True)


def _bind_receipt_paths(
    receipt: Dict,
    evidence_path: Path,
    workspace: Path,
    *,
    stage_dir: Optional[Path],
    patch_path: Optional[Path],
    receipt_path: Path,
):
    if receipt["receipt_path_sha256"] != _path_sha256(receipt_path):
        raise ValidationError("verified stage apply receipt path binding changed")
    if receipt["evidence_path_sha256"] != _path_sha256(evidence_path):
        raise ValidationError("verified stage apply evidence path binding changed")
    if receipt["workspace_path_sha256"] != _path_sha256(workspace):
        raise ValidationError("verified stage apply workspace path binding changed")
    stage = Path(stage_dir) if stage_dir is not None else Path(evidence_path).parent / receipt["stage_directory_name"]
    patch = Path(patch_path) if patch_path is not None else Path(evidence_path).parent / receipt["patch_name"]
    if Path(stage).name != receipt["stage_directory_name"] or _path_sha256(stage) != receipt["stage_path_sha256"]:
        raise ValidationError("verified stage apply stage path binding changed")
    if Path(patch).name != receipt["patch_name"] or _path_sha256(patch) != receipt["patch_path_sha256"]:
        raise ValidationError("verified stage apply patch path binding changed")
    _validate_bound_paths(workspace, evidence_path, stage, patch, receipt_path)
    return stage, patch


def _require_snapshot_binding(snapshot, manifest: Dict, fingerprint: str, excluded, label: str) -> None:
    expected = workspace_snapshot_from_manifest(manifest)
    if (
        snapshot.entries != expected.entries
        or snapshot.tracked_fingerprint_sha256 != expected.tracked_fingerprint_sha256
        or snapshot.fingerprint_sha256 != fingerprint
        or snapshot.excluded_directories != tuple(excluded)
    ):
        raise ValidationError("%s fingerprint binding changed" % label)


def _transition_receipt(
    receipt: Dict,
    status: str,
    *,
    source_after_fingerprint_sha256: Optional[str] = None,
) -> Dict:
    validate_staged_apply_receipt(receipt)
    allowed = {"prepared": "merging", "merging": "merged"}
    if allowed.get(receipt["status"]) != status:
        raise ValidationError("verified stage apply receipt transition is invalid")
    candidate = json.loads(json.dumps(receipt))
    candidate["status"] = status
    candidate["updated_at_utc"] = utc_now()
    candidate["source_after_fingerprint_sha256"] = source_after_fingerprint_sha256
    return _finalize_receipt(candidate)


def _finalize_receipt(receipt: Dict) -> Dict:
    candidate = dict(receipt)
    candidate["receipt_sha256"] = staged_apply_receipt_sha256(candidate)
    validate_staged_apply_receipt(candidate)
    return candidate


def _write_new_staged_apply_receipt(path: Path, receipt: Dict) -> None:
    _require_receipt_path_binding(path, receipt)
    serialized = _serialize_receipt(receipt)
    try:
        write_new_text_file_no_follow(
            path,
            "verified stage apply receipt",
            serialized,
            sync=True,
        )
    except FileExistsError:
        raise ValidationError("verified stage apply receipt already exists")


def _replace_staged_apply_receipt(path: Path, receipt: Dict) -> None:
    _require_receipt_path_binding(path, receipt)
    load_staged_apply_receipt(path)
    replace_text_file_no_follow(
        path,
        "verified stage apply receipt",
        _serialize_receipt(receipt),
        ".staged-apply-",
        sync=True,
    )


def _serialize_receipt(receipt: Dict) -> str:
    validate_staged_apply_receipt(receipt)
    serialized = json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_STAGED_APPLY_RECEIPT_BYTES:
        raise ValidationError("verified stage apply receipt exceeds its byte limit")
    return serialized


def _require_receipt_path_binding(path: Path, receipt: Dict) -> None:
    validate_staged_apply_receipt(receipt)
    if receipt["receipt_path_sha256"] != _path_sha256(path):
        raise ValidationError("verified stage apply receipt path binding changed")


def _require_apply_policy(policy: RuntimePolicy, *, destructive: bool) -> None:
    if not isinstance(policy, RuntimePolicy):
        raise ValidationError("verified stage apply requires a runtime policy")
    if policy.allow_network or policy.allow_agent or policy.allow_parallel:
        raise PolicyError("verified stage apply is local and does not accept network, agent, or parallel capability")
    if not policy.allow_writes:
        raise PolicyError("verified stage apply requires --allow-writes")
    if not policy.has_approval(STAGED_APPLY_APPROVAL):
        raise PolicyError("verified stage apply requires --approve %s" % STAGED_APPLY_APPROVAL)
    if destructive:
        if not policy.allow_destructive:
            raise PolicyError("verified stage deletions require --allow-destructive")
        if not policy.has_approval(STAGED_APPLY_DELETE_APPROVAL):
            raise PolicyError(
                "verified stage deletions require --approve %s"
                % STAGED_APPLY_DELETE_APPROVAL
            )


def _validate_receipt_location(workspace: Path, receipt_path: Path) -> None:
    reject_symlink_path(receipt_path, "verified stage apply receipt")
    require_path_outside_workspace(workspace, receipt_path, "verified stage apply receipt")
    lock_path = receipt_path.with_name(receipt_path.name + ".lock")
    reject_symlink_path(lock_path, "verified stage apply lock")
    require_path_outside_workspace(workspace, lock_path, "verified stage apply lock")


def _validate_initial_paths(
    workspace: Path,
    evidence_path: Path,
    stage: Path,
    patch: Path,
    receipt: Path,
) -> None:
    reject_symlink_path(workspace, "verified stage apply workspace")
    for candidate, label in (
        (evidence_path, "verified stage evidence"),
        (stage, "verified stage directory"),
        (patch, "verified stage patch"),
        (receipt, "verified stage apply receipt"),
    ):
        reject_symlink_path(candidate, label)
        require_path_outside_workspace(workspace, candidate, label)
    _validate_bound_paths(workspace, evidence_path, stage, patch, receipt)


def _validate_bound_paths(
    workspace: Path,
    evidence_path: Path,
    stage: Path,
    patch: Path,
    receipt: Path,
) -> None:
    canonical = {
        "workspace": _canonical_path(workspace),
        "evidence": _canonical_path(evidence_path),
        "stage": _canonical_path(stage),
        "patch": _canonical_path(patch),
        "receipt": _canonical_path(receipt),
        "lock": _canonical_path(receipt.with_name(receipt.name + ".lock")),
    }
    if len(set(canonical.values())) != len(canonical):
        raise ValidationError("verified stage apply paths must be distinct")
    require_path_outside_workspace(workspace, receipt.with_name(receipt.name + ".lock"), "verified stage apply lock")
    for label in ("evidence", "patch", "receipt", "lock"):
        if _is_relative_to(canonical[label], canonical["stage"]):
            raise ValidationError("verified stage %s must be outside the stage directory" % label)


@contextmanager
def _staged_apply_lock(receipt_path: Path):
    if fcntl is None:
        raise ValidationError("verified stage apply locking is unavailable")
    directory_fd = ensure_dir_no_follow(receipt_path.parent, "verified stage apply receipt parent")
    lock_name = receipt_path.name + ".lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = None
    try:
        lock_fd = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError("verified stage apply lock must be a single-link regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ValidationError("failed to lock verified stage apply: %s" % exc.__class__.__name__)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


def _result_summary(receipt: Dict, *, already_applied: bool) -> Dict:
    return {
        "schema": receipt["schema"],
        "status": receipt["status"],
        "already_applied": already_applied,
        "change_count": receipt["change_count"],
        "added": list(receipt["added"]),
        "modified": list(receipt["modified"]),
        "deleted": list(receipt["deleted"]),
        "destructive": receipt["destructive"],
        "provider_calls": 0,
        "verifier_calls": 0,
        "receipt_sha256": receipt["receipt_sha256"],
    }


def _parse_strict_json(raw: bytes, label: str):
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ValidationError("%s is invalid JSON: %s" % (label, exc.__class__.__name__))


def _validate_relative_path_list(value, label: str):
    if not isinstance(value, list) or len(value) > MAX_STAGED_FILES:
        raise ValidationError("%s is invalid" % label)
    observed = []
    for path in value:
        if not isinstance(path, str) or not path or "\\" in path:
            raise ValidationError("%s contains an invalid path" % label)
        require_no_path_escape(path)
        if (
            path.startswith("./")
            or "//" in path
            or path.endswith("/")
            or any(component in {"", ".", ".."} for component in path.split("/"))
        ):
            raise ValidationError("%s contains a non-canonical path" % label)
        observed.append(path)
    if observed != sorted(set(observed)):
        raise ValidationError("%s must be sorted and unique" % label)
    return observed


def _validate_artifact_name(value, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_STAGED_APPLY_NAME_CHARS
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValidationError("%s is invalid" % label)


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(str(_canonical_path(path)).encode("utf-8")).hexdigest()


def _canonical_path(path: Path) -> Path:
    reject_symlink_path(Path(path), "verified stage apply path")
    return Path(path).resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256_json(value) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reject_duplicate_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key %r" % key)
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant %s" % value)
