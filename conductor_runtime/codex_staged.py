import copy
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from .benchmark import (
    BENCHMARK_REPORT_SCHEMA,
    parity_task_execution_guidance,
    parity_task_static_audit_guidance,
    parity_prompt_environment,
    render_parity_task_contract,
    validate_benchmark_report,
    validate_parity_tasks,
)
from .claude_live import CLAUDE_STAGED_WRITE_TOOLS
from .clock import utc_from_timestamp
from .claude_staged import (
    CLAUDE_STAGED_EVIDENCE_SCHEMA,
    MAX_STAGED_EVIDENCE_BYTES,
    MAX_VERIFIER_TIMEOUT_SECONDS,
    VERIFIER_RUNTIME_DIRECTORY_NAMES,
    _bounded_error_text,
    _bounded_int,
    _empty_changes,
    _prepare_verifier,
    _run_verifier,
    _safe_artifact_name,
    _skipped_verification,
    _staged_incidents,
    validate_claude_staged_evidence,
)
from .codex_config import (
    canonicalize_codex_executable_path,
    codex_completion_reserve_guidance,
    codex_small_workspace_write_checkpoint_guidance,
    codex_staged_write_checkpoint_guidance,
)
from .codex_live import (
    CODEX_LIVE_RUN_APPROVAL,
    CODEX_DEFAULT_SERVICE_TIER,
    CODEX_STAGED_PERMISSION_PROFILE,
    DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
    _clean_effort,
    _clean_model,
    _clean_service_tier,
    _clean_tool_output_token_limit,
    _codex_cli_version,
    _parity_task,
    _validate_limits,
    analyze_codex_output,
    build_codex_staged_command,
)
from .errors import PolicyError, ValidationError
from .redaction import redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, ProcessResult, run_process
from .security import (
    RuntimePolicy,
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    write_new_text_file_no_follow,
)
from .staged_workspace import (
    COMPACT_MULTI_FILE_WRITE_GUIDANCE,
    MAX_SMALL_WORKSPACE_CONTEXT_BYTES,
    PUBLIC_CONTRACT_CHECKLIST_GUIDANCE,
    STAGED_POST_WRITE_FINALIZATION_GUIDANCE,
    StagedWorkspaceSnapshot,
    build_small_workspace_context_packet,
    build_staged_completion_summary,
    build_workspace_patch,
    copy_workspace_to_stage,
    render_small_workspace_context_block,
    require_path_outside_workspace,
    snapshot_workspace,
)
from .workflow import SAFE_ID


CODEX_STAGED_EVIDENCE_SCHEMA = "conductor.codex_staged_evidence.v1"
CODEX_STAGED_WRITE_APPROVAL = "codex-staged-write"
CODEX_STAGED_STATUSES = {
    "success",
    "provider-failed",
    "source-drift",
    "invalid-stage",
    "unsupported-binary",
    "unsupported-metadata",
    "no-changes",
    "verification-failed",
    "verification-timed-out",
    "verifier-mutated-stage",
    "patch-failed",
}
CODEX_STAGED_FIELDS = {"schema", "status", "source", "stage", "changes", "verification", "policy", "incidents"}
CODEX_STAGED_POLICY_FIELDS = {
    "source_mutation_allowed",
    "source_mutated",
    "provider_shell_enabled",
    "permission_profile",
    "filesystem_scope",
    "network_access",
    "temporary_writes_allowed",
    "tool_output_token_limit",
    "stage_outside_source",
    "approval_values_persisted",
    "automatic_apply",
}
CODEX_STAGED_INCIDENT_FIELDS = {"id", "severity", "description"}
CODEX_STAGED_INCIDENT_SEVERITIES = {"info", "low", "medium", "high", "critical"}
CODEX_STAGED_FILESYSTEM_SCOPE = "minimal-read-stage-write"
CODEX_STAGED_USABLE_PROVIDER_STATUSES = {"success", "token-budget-exceeded", "workspace-drift"}


def run_codex_staged_task(
    *,
    parity_tasks: Dict,
    task_id: str,
    workspace: Path,
    stage_dir: Path,
    patch_output: Path,
    check_command: List[str],
    policy: RuntimePolicy,
    model: str,
    effort: str,
    max_tokens: int,
    timeout_seconds: int,
    check_timeout_seconds: int,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    check_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    tool_output_token_limit: int = DEFAULT_CODEX_TOOL_OUTPUT_TOKEN_LIMIT,
    service_tier: str = CODEX_DEFAULT_SERVICE_TIER,
    repair_orchestration: bool = False,
) -> Dict:
    public_tasks = {
        key: value for key, value in parity_tasks.items() if not str(key).startswith("_")
    } if isinstance(parity_tasks, dict) else parity_tasks
    validate_parity_tasks(public_tasks)
    task = _parity_task(public_tasks, task_id)
    task_contract = render_parity_task_contract(task)
    if not isinstance(repair_orchestration, bool):
        raise ValidationError("repair_orchestration must be a boolean")
    workspace_input = Path(workspace)
    reject_symlink_path(workspace_input, "Codex staged source workspace")
    workspace_path = workspace_input.resolve()
    stage_path = Path(stage_dir)
    patch_path = Path(patch_output)
    if not workspace_path.is_dir():
        raise ValidationError("Codex staged source workspace must be a directory: %s" % workspace)
    _require_staged_policy(policy)
    if not _safe_artifact_name(stage_path.name):
        raise ValidationError("Codex staged directory must have a safe single-line name")
    if not _safe_artifact_name(patch_path.name):
        raise ValidationError("Codex staged patch output must have a safe single-line name")
    token_cap = _validate_limits(
        task,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )
    tool_limit = _clean_tool_output_token_limit(tool_output_token_limit)
    requested_service_tier = _clean_service_tier(service_tier)
    _bounded_int(check_timeout_seconds, "check_timeout_seconds", 1, MAX_VERIFIER_TIMEOUT_SECONDS)
    _bounded_int(
        check_output_limit_bytes,
        "check_output_limit_bytes",
        1,
        16 * DEFAULT_OUTPUT_LIMIT_BYTES,
    )
    verifier = _prepare_verifier(check_command, stage_path)
    require_path_outside_workspace(workspace_path, stage_path, "stage directory")
    require_path_outside_workspace(workspace_path, patch_path, "staged patch output")
    require_path_outside_workspace(stage_path, patch_path, "staged patch output")
    if patch_path.exists() or patch_path.is_symlink():
        raise ValidationError("staged patch output already exists: %s" % patch_path)
    codex_path = shutil.which("codex")
    if not codex_path:
        raise ValidationError("Codex CLI is not available on PATH")
    codex_path = canonicalize_codex_executable_path(codex_path)
    cli_version = _codex_cli_version(codex_path, workspace_path)
    patch_parent_fd = ensure_dir_no_follow(patch_path.parent, "staged patch output parent")
    try:
        source_before = copy_workspace_to_stage(workspace_path, stage_path)
    finally:
        os.close(patch_parent_fd)
    stage_initial = snapshot_workspace(stage_path)
    if stage_initial.excluded_directories:
        raise ValidationError("Codex staged workspace unexpectedly contains excluded directories")
    workspace_context_packet = build_small_workspace_context_packet(
        stage_path,
        stage_initial,
        max_bytes=min(MAX_SMALL_WORKSPACE_CONTEXT_BYTES, token_cap // 2),
    )
    command = build_codex_staged_command(
        codex_path=codex_path,
        stage=stage_path,
        model=_clean_model(model),
        effort=_clean_effort(effort),
        max_tokens=token_cap,
        tool_output_token_limit=tool_limit,
        service_tier=requested_service_tier,
    )
    prompt = _staged_prompt(
        task_contract,
        task_execution_guidance=parity_task_execution_guidance(task),
        task_static_audit_guidance=parity_task_static_audit_guidance(task),
        repair_orchestration=repair_orchestration,
        max_tokens=token_cap,
        workspace_context_packet=workspace_context_packet,
    )
    prompt_environment = parity_prompt_environment(task_contract, prompt)
    started = time.time()
    started_at_utc = utc_from_timestamp(started).isoformat(timespec="seconds") + "Z"
    try:
        provider_process = run_process(
            command,
            cwd=stage_path,
            timeout=timeout_seconds,
            input_text=prompt,
            output_limit_bytes=output_limit_bytes,
        )
    except OSError as exc:
        provider_process = ProcessResult(
            returncode=127,
            stdout="",
            stderr="Codex staged launch failed: %s" % exc.__class__.__name__,
        )
    provider_duration_ms = max(0, int((time.time() - started) * 1000))

    source_after_provider = None
    stage_before_verification = None
    source_scan_errors = []
    stage_scan_error = None
    try:
        source_after_provider = snapshot_workspace(workspace_path)
    except ValidationError as exc:
        source_scan_errors.append("after provider: %s" % _bounded_error_text(exc))
    try:
        stage_before_verification = snapshot_workspace(stage_path)
        if stage_before_verification.excluded_directories:
            stage_scan_error = "Codex staged output created excluded directories: %s" % ", ".join(
                stage_before_verification.excluded_directories[:10]
            )
    except ValidationError as exc:
        stage_scan_error = _bounded_error_text(exc)
    provider_analysis = analyze_codex_output(
        provider_process,
        cli_version=cli_version,
        model=_clean_model(model),
        effort=_clean_effort(effort),
        max_tokens=token_cap,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        source_before=source_before,
        source_after=source_after_provider,
        stage_before=stage_initial,
        stage_after=stage_before_verification,
        stage_path=stage_path,
        scan_error=" ".join(source_scan_errors + ([stage_scan_error] if stage_scan_error else [])) or None,
        execution_mode="staged-write",
        tool_output_token_limit=tool_limit,
        service_tier=requested_service_tier,
    )
    provider = provider_analysis["provider_evidence"]
    source_gate_unchanged = bool(
        source_after_provider is not None
        and source_before.fingerprint_sha256 == source_after_provider.fingerprint_sha256
    )
    preliminary_changes = _empty_changes()
    change_error = None
    if stage_scan_error is None and stage_before_verification is not None:
        try:
            preliminary_changes = build_workspace_patch(
                workspace_path,
                source_before,
                stage_path,
                stage_before_verification,
            )
        except ValidationError as exc:
            stage_scan_error = _bounded_error_text(exc)

    verification = _skipped_verification("skipped-provider-failed", verifier)
    provider_artifacts_usable = provider["status"] in CODEX_STAGED_USABLE_PROVIDER_STATUSES
    if provider_artifacts_usable and not source_gate_unchanged:
        verification = _skipped_verification("skipped-source-drift", verifier)
    elif provider_artifacts_usable and stage_scan_error:
        verification = _skipped_verification("skipped-invalid-stage", verifier)
    elif provider_artifacts_usable and preliminary_changes["change_count"] == 0:
        verification = _skipped_verification("skipped-no-changes", verifier)
    elif provider_artifacts_usable and preliminary_changes["binary"]:
        verification = _skipped_verification("skipped-unsupported-binary", verifier)
    elif provider_artifacts_usable and (preliminary_changes["mode_changed"] or preliminary_changes["unpatchable"]):
        verification = _skipped_verification("skipped-unsupported-metadata", verifier)
    elif provider_artifacts_usable:
        verification = _run_verifier(
            verifier,
            stage_path,
            timeout_seconds=check_timeout_seconds,
            output_limit_bytes=check_output_limit_bytes,
        )

    stage_after_verification = None
    if stage_scan_error is None:
        try:
            stage_after_verification = snapshot_workspace(
                stage_path,
                extra_excluded_directory_names=VERIFIER_RUNTIME_DIRECTORY_NAMES
                if not verification["status"].startswith("skipped-")
                else None,
            )
        except ValidationError as exc:
            stage_scan_error = _bounded_error_text(exc)
    final_changes = preliminary_changes
    if stage_scan_error is None and stage_after_verification is not None:
        try:
            final_changes = build_workspace_patch(
                workspace_path,
                source_before,
                stage_path,
                stage_after_verification,
            )
        except ValidationError as exc:
            change_error = _bounded_error_text(exc)
            final_changes = _empty_changes(validation_error=change_error)

    verifier_mutated_files = False
    if stage_before_verification is not None and stage_after_verification is not None:
        verifier_mutated_files = (
            stage_before_verification.tracked_fingerprint_sha256
            != stage_after_verification.tracked_fingerprint_sha256
        )
    source_final = None
    try:
        source_final = snapshot_workspace(workspace_path)
    except ValidationError as exc:
        source_scan_errors.append("after verification: %s" % _bounded_error_text(exc))
    source_unchanged = bool(
        source_gate_unchanged
        and source_final is not None
        and source_before.fingerprint_sha256 == source_final.fingerprint_sha256
    )
    source_scan_error = _bounded_error_text(" ".join(source_scan_errors)) if source_scan_errors else None

    patch_written = False
    patch_error = None
    if (
        provider_artifacts_usable
        and source_unchanged
        and stage_scan_error is None
        and change_error is None
        and final_changes["change_count"] > 0
        and not final_changes["binary"]
        and not final_changes["mode_changed"]
        and not final_changes["unpatchable"]
        and not verifier_mutated_files
    ):
        try:
            write_new_text_file_no_follow(
                patch_path,
                "Codex staged patch output",
                final_changes["patch_text"],
            )
            patch_written = True
        except (FileExistsError, OSError, ValidationError) as exc:
            patch_error = _bounded_error_text(exc)

    staged_evidence = build_codex_staged_evidence(
        source_before=source_before,
        source_after_provider=source_after_provider,
        source_final=source_final,
        source_scan_error=source_scan_error,
        stage_before_verification=stage_before_verification,
        stage_after_verification=stage_after_verification,
        stage_path=stage_path,
        patch_path=patch_path,
        changes=final_changes,
        verification=verification,
        source_unchanged=source_unchanged,
        verifier_mutated_files=verifier_mutated_files,
        patch_written=patch_written,
        stage_scan_error=stage_scan_error,
        change_error=change_error,
        patch_error=patch_error,
        provider_status=provider["status"],
        tool_output_token_limit=tool_limit,
    )
    result = {
        "id": task["id"],
        "description": "Pinned, source-isolated Codex staged-write parity run.",
        "passed": provider["status"] == "success" and staged_evidence["status"] == "success",
        "returncode": provider_process.returncode,
        "timed_out": provider_process.timed_out,
        "duration_ms": provider_duration_ms + verification["duration_ms"],
        "stdout_truncated": provider_process.stdout_truncated,
        "stderr_truncated": provider_process.stderr_truncated,
        "stdout": provider_analysis["output_text"],
        "stderr": redact_text(provider_process.stderr),
        "provider_evidence": provider,
        "staged_evidence": staged_evidence,
        "completion_summary": build_staged_completion_summary(final_changes, verification),
    }
    report = {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "suite": public_tasks["name"],
        "suite_source": "configured-parity-tasks",
        "system": "codex-isolated-staged",
        "started_at_utc": started_at_utc,
        "environment": {
            "python": "%s.%s.%s" % sys.version_info[:3],
            "platform": platform.platform(),
            "codex": "available",
            "codex_version": cli_version,
            "external_tool_version_probes": "required",
            **prompt_environment,
        },
        "total_tasks": 1,
        "passed_tasks": 1 if result["passed"] else 0,
        "failed_tasks": 0 if result["passed"] else 1,
        "duration_ms": result["duration_ms"],
        "results": [result],
    }
    validate_benchmark_report(report)
    return report


def build_codex_staged_evidence(
    *,
    source_before: StagedWorkspaceSnapshot,
    source_after_provider: Optional[StagedWorkspaceSnapshot],
    source_final: Optional[StagedWorkspaceSnapshot],
    source_scan_error: Optional[str],
    stage_before_verification: Optional[StagedWorkspaceSnapshot],
    stage_after_verification: Optional[StagedWorkspaceSnapshot],
    stage_path: Path,
    patch_path: Path,
    changes: Dict,
    verification: Dict,
    source_unchanged: bool,
    verifier_mutated_files: bool,
    patch_written: bool,
    stage_scan_error: Optional[str],
    change_error: Optional[str],
    patch_error: Optional[str],
    provider_status: str,
    tool_output_token_limit: int,
) -> Dict:
    stage_snapshot = stage_after_verification or stage_before_verification
    status = _staged_status(
        provider_status=provider_status,
        source_unchanged=source_unchanged,
        stage_scan_error=stage_scan_error,
        change_error=change_error,
        changes=changes,
        verification=verification,
        verifier_mutated_files=verifier_mutated_files,
        patch_written=patch_written,
        patch_error=patch_error,
    )
    evidence = {
        "schema": CODEX_STAGED_EVIDENCE_SCHEMA,
        "status": status,
        "source": {
            "before_fingerprint_sha256": source_before.fingerprint_sha256,
            "after_provider_fingerprint_sha256": source_after_provider.fingerprint_sha256
            if source_after_provider
            else None,
            "final_fingerprint_sha256": source_final.fingerprint_sha256 if source_final else None,
            "file_count": source_before.file_count,
            "total_bytes": source_before.total_bytes,
            "unchanged": source_unchanged,
            "scan_error": source_scan_error,
        },
        "stage": {
            "directory_name": redact_text(Path(stage_path).name),
            "path_sha256": hashlib.sha256(str(Path(stage_path).resolve()).encode("utf-8")).hexdigest(),
            "file_count": stage_snapshot.file_count if stage_snapshot else None,
            "total_bytes": stage_snapshot.total_bytes if stage_snapshot else None,
            "before_verification_sha256": stage_before_verification.tracked_fingerprint_sha256
            if stage_before_verification
            else None,
            "after_verification_sha256": stage_after_verification.tracked_fingerprint_sha256
            if stage_after_verification
            else None,
            "verifier_mutated_files": verifier_mutated_files,
            "persisted": True,
            "scan_error": stage_scan_error,
        },
        "changes": {
            "change_count": changes["change_count"],
            "added": list(changes["added"]),
            "modified": list(changes["modified"]),
            "deleted": list(changes["deleted"]),
            "binary": list(changes["binary"]),
            "mode_changed": list(changes["mode_changed"]),
            "unpatchable": list(changes["unpatchable"]),
            "patch_written": patch_written,
            "patch_name": redact_text(Path(patch_path).name) if patch_written else None,
            "patch_sha256": changes["patch_sha256"] if patch_written else None,
            "patch_bytes": changes["patch_bytes"] if patch_written else 0,
            "validation_error": change_error or changes.get("validation_error"),
            "patch_error": patch_error,
        },
        "verification": verification,
        "policy": _staged_policy(source_unchanged, tool_output_token_limit),
        "incidents": _codex_staged_incidents(
            status,
            source_scan_error=source_scan_error,
            stage_scan_error=stage_scan_error,
            change_error=change_error,
            patch_error=patch_error,
        ),
    }
    validate_codex_staged_evidence(evidence)
    return evidence


def validate_codex_staged_evidence(evidence: Dict, source: str = "<memory>") -> None:
    if not isinstance(evidence, dict):
        raise ValidationError("%s must contain an object" % source)
    try:
        encoded = json.dumps(evidence, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("%s is not JSON-compatible: %s" % (source, exc.__class__.__name__))
    if len(encoded) > MAX_STAGED_EVIDENCE_BYTES:
        raise ValidationError("%s exceeds the staged evidence size limit" % source)
    if set(evidence) != CODEX_STAGED_FIELDS:
        raise ValidationError("%s staged evidence must contain exactly the supported fields" % source)
    if evidence.get("schema") != CODEX_STAGED_EVIDENCE_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, CODEX_STAGED_EVIDENCE_SCHEMA))
    if evidence.get("status") not in CODEX_STAGED_STATUSES:
        raise ValidationError("%s has unsupported staged status" % source)
    policy = evidence.get("policy")
    if not isinstance(policy, dict) or set(policy) != CODEX_STAGED_POLICY_FIELDS:
        raise ValidationError("%s staged policy must contain exactly the supported fields" % source)
    tool_limit = _clean_tool_output_token_limit(policy.get("tool_output_token_limit"))
    source_record = evidence.get("source")
    if not isinstance(source_record, dict) or not isinstance(source_record.get("unchanged"), bool):
        raise ValidationError("%s staged source is invalid" % source)
    expected_policy = _staged_policy(source_record["unchanged"], tool_limit)
    if policy != expected_policy:
        raise ValidationError("%s Codex staged policy metadata is inconsistent" % source)

    # Reuse the mature provider-neutral structural and cross-field checks from
    # the Claude staged contract, substituting only provider-specific metadata.
    shadow = copy.deepcopy(evidence)
    shadow["schema"] = CLAUDE_STAGED_EVIDENCE_SCHEMA
    shadow["policy"] = {
        "source_mutation_allowed": False,
        "source_mutated": not source_record["unchanged"],
        "claude_shell_enabled": False,
        "tools": list(CLAUDE_STAGED_WRITE_TOOLS),
        "stage_outside_source": True,
        "approval_values_persisted": False,
        "automatic_apply": False,
    }
    shadow["incidents"] = _staged_incidents(
        evidence["status"],
        source_scan_error=source_record.get("scan_error"),
        stage_scan_error=evidence.get("stage", {}).get("scan_error"),
        change_error=evidence.get("changes", {}).get("validation_error"),
        patch_error=evidence.get("changes", {}).get("patch_error"),
    )
    validate_claude_staged_evidence(shadow, source=source)

    incidents = evidence.get("incidents")
    if not isinstance(incidents, list) or len(incidents) > 20:
        raise ValidationError("%s incidents must be a bounded list" % source)
    seen = set()
    for incident in incidents:
        if not isinstance(incident, dict) or set(incident) != CODEX_STAGED_INCIDENT_FIELDS:
            raise ValidationError("%s staged incident must contain exactly the supported fields" % source)
        incident_id = incident.get("id")
        if not isinstance(incident_id, str) or not SAFE_ID.match(incident_id) or incident_id in seen:
            raise ValidationError("%s incident ids must be unique safe identifiers" % source)
        seen.add(incident_id)
        if incident.get("severity") not in CODEX_STAGED_INCIDENT_SEVERITIES:
            raise ValidationError("%s incident severity is invalid" % source)
        description = incident.get("description")
        if not isinstance(description, str) or not description or len(description) > 8192:
            raise ValidationError("%s incident description must be bounded non-empty text" % source)
    expected_incidents = _codex_staged_incidents(
        evidence["status"],
        source_scan_error=source_record.get("scan_error"),
        stage_scan_error=evidence["stage"].get("scan_error"),
        change_error=evidence["changes"].get("validation_error"),
        patch_error=evidence["changes"].get("patch_error"),
    )
    if incidents != expected_incidents:
        raise ValidationError("%s staged incidents are inconsistent" % source)


def load_codex_staged_evidence(path: Path) -> Dict:
    text = read_regular_text_file_no_follow(
        Path(path),
        "Codex staged evidence",
        max_bytes=MAX_STAGED_EVIDENCE_BYTES,
    )
    try:
        evidence = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("%s is not valid Codex staged evidence JSON: %s" % (path, exc))
    validate_codex_staged_evidence(evidence, source=str(path))
    return evidence


def _staged_status(
    *,
    provider_status: str,
    source_unchanged: bool,
    stage_scan_error: Optional[str],
    change_error: Optional[str],
    changes: Dict,
    verification: Dict,
    verifier_mutated_files: bool,
    patch_written: bool,
    patch_error: Optional[str],
) -> str:
    if not source_unchanged:
        return "source-drift"
    if stage_scan_error or change_error:
        return "invalid-stage"
    if provider_status not in CODEX_STAGED_USABLE_PROVIDER_STATUSES:
        return "provider-failed"
    if verifier_mutated_files:
        return "verifier-mutated-stage"
    if changes["binary"]:
        return "unsupported-binary"
    if changes["mode_changed"] or changes["unpatchable"]:
        return "unsupported-metadata"
    if changes["change_count"] == 0:
        return "no-changes"
    if patch_error or not patch_written:
        return "patch-failed"
    if verification["status"] == "timed-out":
        return "verification-timed-out"
    if verification["status"] != "passed":
        return "verification-failed"
    return "success"


def _staged_policy(source_unchanged: bool, tool_output_token_limit: int) -> Dict:
    tool_limit = _clean_tool_output_token_limit(tool_output_token_limit)
    return {
        "source_mutation_allowed": False,
        "source_mutated": not source_unchanged,
        "provider_shell_enabled": True,
        "permission_profile": CODEX_STAGED_PERMISSION_PROFILE,
        "filesystem_scope": CODEX_STAGED_FILESYSTEM_SCOPE,
        "network_access": False,
        "temporary_writes_allowed": False,
        "tool_output_token_limit": tool_limit,
        "stage_outside_source": True,
        "approval_values_persisted": False,
        "automatic_apply": False,
    }


def _codex_staged_incidents(
    status: str,
    *,
    source_scan_error: Optional[str],
    stage_scan_error: Optional[str],
    change_error: Optional[str],
    patch_error: Optional[str],
) -> List[Dict]:
    mapping = {
        "provider-failed": ("provider-failed", "medium", "Codex provider execution did not complete successfully."),
        "source-drift": ("source-drift", "high", "The source workspace changed while the Codex staged run was active."),
        "invalid-stage": (
            "invalid-stage",
            "high",
            "The Codex staged workspace or generated change set failed strict validation.",
        ),
        "unsupported-binary": (
            "unsupported-binary",
            "medium",
            "The Codex change set contains binary or non-UTF-8 files that cannot be emitted as a text patch.",
        ),
        "unsupported-metadata": (
            "unsupported-metadata",
            "medium",
            "The Codex change set contains executable-bit or unsupported metadata changes.",
        ),
        "no-changes": ("no-changes", "low", "Codex completed without producing staged file changes."),
        "verification-failed": (
            "verification-failed",
            "medium",
            "The deterministic Codex staged verifier exited nonzero.",
        ),
        "verification-timed-out": (
            "verification-timed-out",
            "medium",
            "The deterministic Codex staged verifier exceeded its timeout.",
        ),
        "verifier-mutated-stage": (
            "verifier-mutated-stage",
            "high",
            "The verifier changed tracked staged files, so Codex authorship and patch evidence are ambiguous.",
        ),
        "patch-failed": ("patch-failed", "high", "The validated Codex staged patch could not be persisted."),
    }
    if status == "success":
        return []
    incident_id, severity, description = mapping[status]
    detail = source_scan_error or stage_scan_error or change_error or patch_error
    if detail:
        description += " %s" % redact_text(detail)
    return [{"id": incident_id, "severity": severity, "description": description}]


def _require_staged_policy(policy: RuntimePolicy) -> None:
    if not policy.allow_agent:
        raise PolicyError("Codex staged parity requires --allow-agent")
    if not policy.allow_network:
        raise PolicyError("Codex staged parity requires --allow-network")
    if not policy.allow_writes:
        raise PolicyError("Codex staged parity requires --allow-writes")
    if policy.allow_destructive or policy.allow_parallel:
        raise PolicyError("Codex staged parity forbids destructive and parallel capabilities")
    if not policy.has_approval(CODEX_LIVE_RUN_APPROVAL):
        raise PolicyError("Codex staged parity requires --approve %s" % CODEX_LIVE_RUN_APPROVAL)
    if not policy.has_approval(CODEX_STAGED_WRITE_APPROVAL):
        raise PolicyError("Codex staged parity requires --approve %s" % CODEX_STAGED_WRITE_APPROVAL)


def _staged_prompt(
    task_prompt: str,
    *,
    task_execution_guidance: str,
    task_static_audit_guidance: str,
    repair_orchestration: bool,
    max_tokens: int,
    workspace_context_packet: Optional[str],
) -> str:
    if repair_orchestration:
        verification_guidance = (
            "Do not run tests, language runtimes, toolchain probes, or diffs in this turn. Use the bounded feedback "
            "and current workspace for all required writes; the deterministic verifier follows and repair "
            "orchestration may supply a later attempt."
        )
    else:
        verification_guidance = (
            "Do not run tests, language runtimes, toolchain probes, or diffs in this turn. The authoritative "
            "deterministic verifier follows, with no correction turn; account for the full contract in the writes."
        )
    guidance_block = ""
    if task_execution_guidance:
        guidance_block = "\n\nConductor task-type policy:\n%s\n" % task_execution_guidance
    static_audit_guidance = task_static_audit_guidance
    if not static_audit_guidance and not task_execution_guidance:
        static_audit_guidance = PUBLIC_CONTRACT_CHECKLIST_GUIDANCE
    static_audit_block = (
        "\n\nConductor pre-write contract checklist:\n%s\n" % static_audit_guidance
        if static_audit_guidance
        else ""
    )
    if workspace_context_packet is None:
        inspection_guidance = (
            "Use the fewest read-only commands needed for affected definitions and direct callers; do not defer "
            "writes for broad analysis. "
        )
        write_checkpoint_guidance = codex_staged_write_checkpoint_guidance(max_tokens)
    else:
        inspection_guidance = COMPACT_MULTI_FILE_WRITE_GUIDANCE
        write_checkpoint_guidance = codex_small_workspace_write_checkpoint_guidance(max_tokens)
    return (
        "Implement the benchmark task in the isolated stage. This adapter is the full workflow; external skills, "
        "plugins, goals, and orchestration are out of scope. Follow the developer and permission contracts. Edit "
        "only required files; do not install dependencies or access external paths or network tools. %sComplete all "
        "affected files. "
        "%s"
        "%s"
        "%s"
        "\n\n"
        "BEGIN_UNTRUSTED_TASK\n%s\nEND_UNTRUSTED_TASK\n"
        "%s"
        "%s"
        "%s"
        "%s"
    ) % (
        inspection_guidance,
        codex_completion_reserve_guidance(max_tokens, exact_cap=True),
        write_checkpoint_guidance,
        verification_guidance,
        task_prompt,
        render_small_workspace_context_block(workspace_context_packet),
        static_audit_block,
        guidance_block,
        STAGED_POST_WRITE_FINALIZATION_GUIDANCE,
    )


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)
