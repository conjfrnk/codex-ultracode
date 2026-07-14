import hashlib
import json
import math
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
from .clock import utc_from_timestamp
from .claude_live import (
    CLAUDE_EFFORT,  # noqa: F401 - compatibility re-export
    CLAUDE_MODEL,  # noqa: F401 - compatibility re-export
    CLAUDE_PAID_RUN_APPROVAL,
    CLAUDE_STAGED_PERMISSION_MODE,
    CLAUDE_STAGED_WRITE_TOOLS,
    MAX_CLAUDE_TURNS,
    _claude_cli_version,
    analyze_claude_output,
    build_claude_command,
)
from .errors import PolicyError, ValidationError
from .redaction import redact_text
from .runner import DEFAULT_OUTPUT_LIMIT_BYTES, ProcessResult, run_process
from .security import (
    RuntimePolicy,
    enforce_shell_policy,
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    write_new_text_file_no_follow,
)
from .staged_workspace import (
    MAX_SMALL_WORKSPACE_CONTEXT_BYTES,
    MAX_STAGED_CHANGES,
    MAX_STAGED_FILES,
    MAX_STAGED_PATCH_BYTES,
    MAX_STAGED_TOTAL_BYTES,
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
from .workflow import MAX_TIMEOUT_SECONDS, SAFE_ID


CLAUDE_STAGED_EVIDENCE_SCHEMA = "conductor.claude_staged_evidence.v1"
CLAUDE_STAGED_WRITE_APPROVAL = "claude-staged-write"
CLAUDE_STAGED_STATUSES = {
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
CLAUDE_STAGED_INCIDENT_SEVERITIES = {"info", "low", "medium", "high", "critical"}
CLAUDE_STAGED_FIELDS = {"schema", "status", "source", "stage", "changes", "verification", "policy", "incidents"}
CLAUDE_STAGED_SOURCE_FIELDS = {
    "before_fingerprint_sha256",
    "after_provider_fingerprint_sha256",
    "final_fingerprint_sha256",
    "file_count",
    "total_bytes",
    "unchanged",
    "scan_error",
}
CLAUDE_STAGED_STAGE_FIELDS = {
    "directory_name",
    "path_sha256",
    "file_count",
    "total_bytes",
    "before_verification_sha256",
    "after_verification_sha256",
    "verifier_mutated_files",
    "persisted",
    "scan_error",
}
CLAUDE_STAGED_CHANGE_FIELDS = {
    "change_count",
    "added",
    "modified",
    "deleted",
    "binary",
    "mode_changed",
    "unpatchable",
    "patch_written",
    "patch_name",
    "patch_sha256",
    "patch_bytes",
    "validation_error",
    "patch_error",
}
CLAUDE_STAGED_VERIFICATION_FIELDS = {
    "configured",
    "status",
    "returncode",
    "timed_out",
    "duration_ms",
    "stdout_bytes",
    "stderr_bytes",
    "stdout_truncated",
    "stderr_truncated",
    "stdout_excerpt",
    "stderr_excerpt",
    "command_sha256",
    "argv_count",
    "sandbox",
    "environment_sanitized",
    "network_isolated",
    "write_scope",
}
CLAUDE_STAGED_POLICY_FIELDS = {
    "source_mutation_allowed",
    "source_mutated",
    "claude_shell_enabled",
    "tools",
    "stage_outside_source",
    "approval_values_persisted",
    "automatic_apply",
}
CLAUDE_STAGED_INCIDENT_FIELDS = {"id", "severity", "description"}
CLAUDE_STAGED_VERIFICATION_STATUSES = {
    "passed",
    "failed",
    "timed-out",
    "skipped-provider-failed",
    "skipped-source-drift",
    "skipped-invalid-stage",
    "skipped-unsupported-binary",
    "skipped-unsupported-metadata",
    "skipped-no-changes",
}
MAX_STAGED_EVIDENCE_BYTES = 512 * 1024
MAX_VERIFIER_TIMEOUT_SECONDS = 60 * 60
MAX_VERIFIER_EXCERPT_CHARS = 4096
VERIFIER_EXCERPT_OMISSION = "\n...[verifier output omitted]...\n"
CLAUDE_STAGED_VERIFIER_SANDBOXES = {"macos-seatbelt", "linux-bwrap"}
CLAUDE_STAGED_VERIFIER_WRITE_SCOPE = "stage-only"
CLAUDE_STAGED_USABLE_PROVIDER_STATUSES = {"success", "budget-exceeded"}
VERIFIER_RUNTIME_DIRECTORY_NAMES = {".conductor-verifier-home", ".conductor-verifier-tmp"}
_MACOS_VERIFIER_PROFILE = """(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(deny file-read* (subpath (param "USER_HOME")))
(deny file-read* (subpath "/private/tmp"))
(deny file-read* (subpath "/private/var/folders"))
(allow file-read* (subpath (param "STAGE")))
(allow file-write* (subpath (param "STAGE")))
(allow file-write* (literal "/dev/null"))
"""


def run_claude_staged_task(
    *,
    parity_tasks: Dict,
    task_id: str,
    workspace: Path,
    stage_dir: Path,
    patch_output: Path,
    check_command: List[str],
    policy: RuntimePolicy,
    max_budget_usd: float,
    max_turns: int,
    timeout_seconds: int,
    check_timeout_seconds: int,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    check_output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
) -> Dict:
    public_tasks = {
        key: value for key, value in parity_tasks.items() if not str(key).startswith("_")
    } if isinstance(parity_tasks, dict) else parity_tasks
    validate_parity_tasks(public_tasks)
    task = _parity_task(public_tasks, task_id)
    task_contract = render_parity_task_contract(task)
    workspace_input = Path(workspace)
    reject_symlink_path(workspace_input, "Claude staged source workspace")
    workspace_path = workspace_input.resolve()
    stage_path = Path(stage_dir)
    patch_path = Path(patch_output)
    if not workspace_path.is_dir():
        raise ValidationError("Claude staged source workspace must be a directory: %s" % workspace)
    _require_staged_policy(policy)
    if not _safe_artifact_name(stage_path.name):
        raise ValidationError("Claude staged directory must have a safe single-line name")
    if not _safe_artifact_name(patch_path.name):
        raise ValidationError("Claude staged patch output must have a safe single-line name")
    budget = _validate_limits(
        task,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        check_timeout_seconds=check_timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        check_output_limit_bytes=check_output_limit_bytes,
    )
    verifier = _prepare_verifier(check_command, stage_path)
    require_path_outside_workspace(workspace_path, stage_path, "stage directory")
    require_path_outside_workspace(workspace_path, patch_path, "staged patch output")
    require_path_outside_workspace(stage_path, patch_path, "staged patch output")
    if patch_path.exists() or patch_path.is_symlink():
        raise ValidationError("staged patch output already exists: %s" % patch_path)
    claude_path = shutil.which("claude")
    if not claude_path:
        raise ValidationError("Claude Code CLI is not available on PATH")
    cli_version = _claude_cli_version(claude_path, workspace_path)
    patch_parent_fd = ensure_dir_no_follow(patch_path.parent, "staged patch output parent")
    os.close(patch_parent_fd)
    source_before = copy_workspace_to_stage(workspace_path, stage_path)
    stage_initial = snapshot_workspace(stage_path)
    if stage_initial.excluded_directories:
        raise ValidationError("Claude staged workspace unexpectedly contains excluded directories")
    workspace_context_packet = build_small_workspace_context_packet(
        stage_path,
        stage_initial,
        max_bytes=min(
            MAX_SMALL_WORKSPACE_CONTEXT_BYTES,
            task["budget"]["max_tokens"] // 2,
        ),
    )
    command = build_claude_command(
        claude_path=claude_path,
        max_budget_usd=budget,
        max_turns=max_turns,
        permission_mode=CLAUDE_STAGED_PERMISSION_MODE,
        tools=CLAUDE_STAGED_WRITE_TOOLS,
    )
    prompt = _staged_prompt(
        task_contract,
        task_execution_guidance=parity_task_execution_guidance(task),
        task_static_audit_guidance=parity_task_static_audit_guidance(task),
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
            stderr="Claude Code staged launch failed: %s" % exc.__class__.__name__,
        )
    provider_duration_ms = max(0, int((time.time() - started) * 1000))
    provider = analyze_claude_output(
        provider_process,
        cli_version=cli_version,
        max_budget_usd=budget,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        permission_mode=CLAUDE_STAGED_PERMISSION_MODE,
        tools=CLAUDE_STAGED_WRITE_TOOLS,
        read_only_tools=False,
    )

    source_after_provider = None
    source_scan_errors = []
    try:
        source_after_provider = snapshot_workspace(workspace_path)
    except ValidationError as exc:
        source_scan_errors.append("after provider: %s" % _bounded_error_text(exc))
    source_gate_unchanged = bool(
        source_after_provider is not None
        and source_before.fingerprint_sha256 == source_after_provider.fingerprint_sha256
    )
    stage_before_verification = None
    stage_after_verification = None
    stage_scan_error = None
    change_error = None
    preliminary_changes = _empty_changes()
    try:
        stage_before_verification = snapshot_workspace(stage_path)
        if stage_before_verification.excluded_directories:
            raise ValidationError(
                "Claude staged output created excluded directories: %s"
                % ", ".join(stage_before_verification.excluded_directories[:10])
            )
        preliminary_changes = build_workspace_patch(
            workspace_path,
            source_before,
            stage_path,
            stage_before_verification,
        )
    except ValidationError as exc:
        stage_scan_error = _bounded_error_text(exc)

    verification = _skipped_verification("skipped-provider-failed", verifier)
    provider_status = provider["provider_evidence"]["status"]
    provider_success = provider_status == "success"
    provider_artifacts_usable = provider_status in CLAUDE_STAGED_USABLE_PROVIDER_STATUSES
    if provider_artifacts_usable and not source_gate_unchanged:
        verification = _skipped_verification("skipped-source-drift", verifier)
    elif provider_artifacts_usable and stage_scan_error:
        verification = _skipped_verification("skipped-invalid-stage", verifier)
    elif provider_artifacts_usable and preliminary_changes["change_count"] == 0:
        verification = _skipped_verification("skipped-no-changes", verifier)
    elif provider_artifacts_usable and preliminary_changes["binary"]:
        verification = _skipped_verification("skipped-unsupported-binary", verifier)
    elif provider_artifacts_usable and preliminary_changes["mode_changed"]:
        verification = _skipped_verification("skipped-unsupported-metadata", verifier)
    elif provider_artifacts_usable and preliminary_changes["unpatchable"]:
        verification = _skipped_verification("skipped-unsupported-metadata", verifier)
    elif provider_artifacts_usable:
        verification = _run_verifier(
            verifier,
            stage_path,
            timeout_seconds=check_timeout_seconds,
            output_limit_bytes=check_output_limit_bytes,
        )

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
                "Claude staged patch output",
                final_changes["patch_text"],
            )
            patch_written = True
        except (FileExistsError, OSError, ValidationError) as exc:
            patch_error = _bounded_error_text(exc)

    staged_evidence = build_claude_staged_evidence(
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
        provider_status=provider_status,
    )
    result = {
        "id": task["id"],
        "description": "Pinned Claude Sonnet Ultracode staged-write parity run.",
        "passed": provider_success and staged_evidence["status"] == "success",
        "returncode": provider_process.returncode,
        "timed_out": provider_process.timed_out,
        "duration_ms": provider_duration_ms + verification["duration_ms"],
        "stdout_truncated": provider_process.stdout_truncated,
        "stderr_truncated": provider_process.stderr_truncated,
        "stdout": provider["output_text"],
        "stderr": redact_text(provider_process.stderr),
        "provider_evidence": provider["provider_evidence"],
        "staged_evidence": staged_evidence,
        "completion_summary": build_staged_completion_summary(final_changes, verification),
    }
    report = {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "suite": public_tasks["name"],
        "suite_source": "configured-parity-tasks",
        "system": "claude-sonnet-ultracode-staged",
        "started_at_utc": started_at_utc,
        "environment": {
            "python": "%s.%s.%s" % sys.version_info[:3],
            "platform": platform.platform(),
            "claude": "available",
            "claude_version": cli_version,
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


def build_claude_staged_evidence(
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
        "schema": CLAUDE_STAGED_EVIDENCE_SCHEMA,
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
        "policy": {
            "source_mutation_allowed": False,
            "source_mutated": not source_unchanged,
            "claude_shell_enabled": False,
            "tools": list(CLAUDE_STAGED_WRITE_TOOLS),
            "stage_outside_source": True,
            "approval_values_persisted": False,
            "automatic_apply": False,
        },
        "incidents": _staged_incidents(
            status,
            source_scan_error=source_scan_error,
            stage_scan_error=stage_scan_error,
            change_error=change_error,
            patch_error=patch_error,
        ),
    }
    validate_claude_staged_evidence(evidence)
    return evidence


def validate_claude_staged_evidence(evidence: Dict, source: str = "<memory>") -> None:
    if not isinstance(evidence, dict):
        raise ValidationError("%s must contain an object" % source)
    try:
        encoded_evidence = json.dumps(evidence, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("%s is not JSON-compatible: %s" % (source, exc.__class__.__name__))
    if len(encoded_evidence) > MAX_STAGED_EVIDENCE_BYTES:
        raise ValidationError("%s exceeds the staged evidence size limit" % source)
    _exact_fields(evidence, CLAUDE_STAGED_FIELDS, "%s staged evidence" % source)
    if evidence.get("schema") != CLAUDE_STAGED_EVIDENCE_SCHEMA:
        raise ValidationError("%s must set schema to %s" % (source, CLAUDE_STAGED_EVIDENCE_SCHEMA))
    status = evidence.get("status")
    if status not in CLAUDE_STAGED_STATUSES:
        raise ValidationError("%s has unsupported staged status" % source)
    source_record = _strict_object(evidence.get("source"), CLAUDE_STAGED_SOURCE_FIELDS, "%s source" % source)
    stage = _strict_object(evidence.get("stage"), CLAUDE_STAGED_STAGE_FIELDS, "%s stage" % source)
    changes = _strict_object(evidence.get("changes"), CLAUDE_STAGED_CHANGE_FIELDS, "%s changes" % source)
    verification = _strict_object(
        evidence.get("verification"), CLAUDE_STAGED_VERIFICATION_FIELDS, "%s verification" % source
    )
    policy = _strict_object(evidence.get("policy"), CLAUDE_STAGED_POLICY_FIELDS, "%s policy" % source)
    _sha256(source_record.get("before_fingerprint_sha256"), "%s source before_fingerprint_sha256" % source)
    for field in ["after_provider_fingerprint_sha256", "final_fingerprint_sha256"]:
        if source_record.get(field) is not None:
            _sha256(source_record[field], "%s source %s" % (source, field))
    _bounded_int(source_record.get("file_count"), "%s source file_count" % source, 0, MAX_STAGED_FILES)
    _bounded_int(source_record.get("total_bytes"), "%s source total_bytes" % source, 0, MAX_STAGED_TOTAL_BYTES)
    if not isinstance(source_record.get("unchanged"), bool):
        raise ValidationError("%s source unchanged must be boolean" % source)
    _optional_error(source_record.get("scan_error"), "%s source scan_error" % source)
    if (
        source_record["after_provider_fingerprint_sha256"] is None
        or source_record["final_fingerprint_sha256"] is None
    ) and source_record["scan_error"] is None:
        raise ValidationError("%s missing source fingerprints require a scan error" % source)
    expected_source_unchanged = (
        source_record["after_provider_fingerprint_sha256"] is not None
        and source_record["final_fingerprint_sha256"] is not None
        and source_record["scan_error"] is None
        and source_record["before_fingerprint_sha256"]
        == source_record["after_provider_fingerprint_sha256"]
        == source_record["final_fingerprint_sha256"]
    )
    if source_record["unchanged"] != expected_source_unchanged:
        raise ValidationError("%s source fingerprint summary is inconsistent" % source)
    if not _safe_artifact_name(stage.get("directory_name")):
        raise ValidationError("%s stage directory_name must be non-empty" % source)
    _sha256(stage.get("path_sha256"), "%s stage path_sha256" % source)
    if stage.get("file_count") is not None:
        _bounded_int(stage["file_count"], "%s stage file_count" % source, 0, MAX_STAGED_FILES)
    if stage.get("total_bytes") is not None:
        _bounded_int(stage["total_bytes"], "%s stage total_bytes" % source, 0, MAX_STAGED_TOTAL_BYTES)
    for field in ["before_verification_sha256", "after_verification_sha256"]:
        if stage.get(field) is not None:
            _sha256(stage[field], "%s stage %s" % (source, field))
    for field in ["verifier_mutated_files", "persisted"]:
        if not isinstance(stage.get(field), bool):
            raise ValidationError("%s stage %s must be boolean" % (source, field))
    _optional_error(stage.get("scan_error"), "%s stage scan_error" % source)
    if stage["persisted"] is not True:
        raise ValidationError("%s staged workspace must be retained" % source)
    if stage["scan_error"] is None and (
        stage["before_verification_sha256"] is None or stage["after_verification_sha256"] is None
    ):
        raise ValidationError("%s successful stage scans require before and after fingerprints" % source)
    expected_verifier_mutation = (
        stage.get("before_verification_sha256") is not None
        and stage.get("after_verification_sha256") is not None
        and stage["before_verification_sha256"] != stage["after_verification_sha256"]
    )
    if stage["verifier_mutated_files"] != expected_verifier_mutation:
        raise ValidationError("%s verifier mutation summary is inconsistent" % source)
    path_lists = {}
    for field in ["added", "modified", "deleted", "binary", "mode_changed", "unpatchable"]:
        path_lists[field] = _path_list(changes.get(field), "%s changes %s" % (source, field))
    if set(path_lists["added"]) & set(path_lists["modified"] + path_lists["deleted"]):
        raise ValidationError("%s change path sets must be disjoint" % source)
    if set(path_lists["modified"]) & set(path_lists["deleted"]):
        raise ValidationError("%s change path sets must be disjoint" % source)
    expected_count = len(path_lists["added"]) + len(path_lists["modified"]) + len(path_lists["deleted"])
    if changes.get("change_count") != expected_count:
        raise ValidationError("%s change_count is inconsistent" % source)
    _bounded_int(changes.get("change_count"), "%s change_count" % source, 0, MAX_STAGED_CHANGES)
    changed_paths = set(path_lists["added"] + path_lists["modified"] + path_lists["deleted"])
    if not set(path_lists["binary"]).issubset(changed_paths):
        raise ValidationError("%s binary paths must be changed paths" % source)
    if not set(path_lists["mode_changed"]).issubset(set(path_lists["added"] + path_lists["modified"])):
        raise ValidationError("%s mode_changed paths must be added or modified paths" % source)
    if not set(path_lists["unpatchable"]).issubset(changed_paths):
        raise ValidationError("%s unpatchable paths must be changed paths" % source)
    for field in ["patch_written"]:
        if not isinstance(changes.get(field), bool):
            raise ValidationError("%s changes %s must be boolean" % (source, field))
    if changes["patch_written"]:
        if not _safe_artifact_name(changes.get("patch_name")):
            raise ValidationError("%s patch_name must be non-empty when written" % source)
        _sha256(changes.get("patch_sha256"), "%s patch_sha256" % source)
        _bounded_int(changes.get("patch_bytes"), "%s patch_bytes" % source, 1, MAX_STAGED_PATCH_BYTES)
    elif changes.get("patch_name") is not None or changes.get("patch_sha256") is not None or changes.get("patch_bytes") != 0:
        raise ValidationError("%s unwritten patch metadata is inconsistent" % source)
    for field in ["validation_error", "patch_error"]:
        _optional_error(changes.get(field), "%s changes %s" % (source, field))
    _validate_verification(verification, source)
    expected_policy = {
        "source_mutation_allowed": False,
        "source_mutated": not source_record["unchanged"],
        "claude_shell_enabled": False,
        "tools": CLAUDE_STAGED_WRITE_TOOLS,
        "stage_outside_source": True,
        "approval_values_persisted": False,
        "automatic_apply": False,
    }
    if policy != expected_policy:
        raise ValidationError("%s staged policy metadata is inconsistent" % source)
    expected_status = _status_from_evidence(source_record, stage, changes, verification)
    if status != expected_status:
        raise ValidationError("%s staged status is inconsistent with evidence" % source)
    _validate_staged_cross_fields(status, source_record, stage, changes, verification, source)
    incidents = evidence.get("incidents")
    if not isinstance(incidents, list) or len(incidents) > 20:
        raise ValidationError("%s incidents must be a bounded list" % source)
    seen_incidents = set()
    for incident in incidents:
        incident = _strict_object(incident, CLAUDE_STAGED_INCIDENT_FIELDS, "%s incident" % source)
        incident_id = incident.get("id")
        if not isinstance(incident_id, str) or not SAFE_ID.match(incident_id) or incident_id in seen_incidents:
            raise ValidationError("%s incident ids must be unique safe identifiers" % source)
        seen_incidents.add(incident_id)
        if incident.get("severity") not in CLAUDE_STAGED_INCIDENT_SEVERITIES:
            raise ValidationError("%s incident severity is invalid" % source)
        if not isinstance(incident.get("description"), str) or not incident["description"] or len(incident["description"]) > 8192:
            raise ValidationError("%s incident description must be bounded non-empty text" % source)
    expected_incidents = _staged_incidents(
        status,
        source_scan_error=source_record.get("scan_error"),
        stage_scan_error=stage.get("scan_error"),
        change_error=changes.get("validation_error"),
        patch_error=changes.get("patch_error"),
    )
    if incidents != expected_incidents:
        raise ValidationError("%s staged incidents are inconsistent" % source)


def load_claude_staged_evidence(path: Path) -> Dict:
    text = read_regular_text_file_no_follow(
        Path(path),
        "Claude staged evidence",
        max_bytes=MAX_STAGED_EVIDENCE_BYTES,
    )
    try:
        evidence = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("%s is not valid Claude staged evidence JSON: %s" % (path, exc))
    validate_claude_staged_evidence(evidence, source=str(path))
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
    if provider_status not in CLAUDE_STAGED_USABLE_PROVIDER_STATUSES:
        return "provider-failed"
    if not source_unchanged:
        return "source-drift"
    if stage_scan_error or change_error:
        return "invalid-stage"
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


def _status_from_evidence(source: Dict, stage: Dict, changes: Dict, verification: Dict) -> str:
    # A skipped-provider status is the durable indication that provider evidence failed.
    if verification["status"] == "skipped-provider-failed":
        return "provider-failed"
    if not source["unchanged"]:
        return "source-drift"
    if stage.get("scan_error") or changes.get("validation_error"):
        return "invalid-stage"
    if stage["verifier_mutated_files"]:
        return "verifier-mutated-stage"
    if changes["binary"]:
        return "unsupported-binary"
    if changes["mode_changed"] or changes["unpatchable"]:
        return "unsupported-metadata"
    if changes["change_count"] == 0:
        return "no-changes"
    if changes.get("patch_error") or not changes["patch_written"]:
        return "patch-failed"
    if verification["status"] == "timed-out":
        return "verification-timed-out"
    if verification["status"] != "passed":
        return "verification-failed"
    return "success"


def _validate_staged_cross_fields(
    status: str,
    source_record: Dict,
    stage: Dict,
    changes: Dict,
    verification: Dict,
    source: str,
) -> None:
    verification_status = verification["status"]
    exact_verification = {
        "provider-failed": "skipped-provider-failed",
        "unsupported-binary": "skipped-unsupported-binary",
        "unsupported-metadata": "skipped-unsupported-metadata",
        "no-changes": "skipped-no-changes",
        "verification-failed": "failed",
        "verification-timed-out": "timed-out",
        "success": "passed",
    }
    expected_verification = exact_verification.get(status)
    if expected_verification is not None and verification_status != expected_verification:
        raise ValidationError("%s staged verification does not match status" % source)
    if status == "source-drift" and verification_status not in {
        "skipped-source-drift",
        "passed",
        "failed",
        "timed-out",
    }:
        raise ValidationError("%s source-drift verification metadata is inconsistent" % source)
    if status == "invalid-stage" and verification_status not in {
        "skipped-invalid-stage",
        "passed",
        "failed",
        "timed-out",
    }:
        raise ValidationError("%s invalid-stage verification metadata is inconsistent" % source)
    if status == "verifier-mutated-stage" and verification_status not in {"passed", "failed", "timed-out"}:
        raise ValidationError("%s verifier mutation requires an executed verifier" % source)
    if status == "patch-failed" and verification_status not in {"passed", "failed", "timed-out"}:
        raise ValidationError("%s patch failure requires an executed verifier" % source)

    if status == "invalid-stage" and not (stage["scan_error"] or changes["validation_error"]):
        raise ValidationError("%s invalid-stage status requires a validation error" % source)
    if status == "unsupported-binary" and not changes["binary"]:
        raise ValidationError("%s unsupported-binary status requires binary paths" % source)
    if status == "unsupported-metadata" and not (changes["mode_changed"] or changes["unpatchable"]):
        raise ValidationError("%s unsupported-metadata status requires unsupported paths" % source)
    if status == "verifier-mutated-stage" and not stage["verifier_mutated_files"]:
        raise ValidationError("%s verifier-mutated-stage status requires tracked mutation" % source)

    if changes["patch_error"] is not None and status != "patch-failed":
        raise ValidationError("%s patch errors are only valid for patch-failed evidence" % source)
    if status == "patch-failed" and changes["patch_error"] is None:
        raise ValidationError("%s patch-failed evidence must record the write error" % source)
    if changes["patch_written"]:
        if status not in {"success", "verification-failed", "verification-timed-out"}:
            raise ValidationError("%s patch persistence is inconsistent with staged status" % source)
        if (
            not source_record["unchanged"]
            or stage["scan_error"] is not None
            or changes["validation_error"] is not None
            or changes["binary"]
            or changes["mode_changed"]
            or changes["unpatchable"]
            or stage["verifier_mutated_files"]
            or changes["change_count"] == 0
        ):
            raise ValidationError("%s persisted patch violates the staged write contract" % source)
    elif status in {"success", "verification-failed", "verification-timed-out"}:
        raise ValidationError("%s staged status requires a persisted patch" % source)


def _staged_incidents(
    status: str,
    *,
    source_scan_error: Optional[str],
    stage_scan_error: Optional[str],
    change_error: Optional[str],
    patch_error: Optional[str],
) -> List[Dict]:
    mapping = {
        "provider-failed": ("provider-failed", "medium", "Claude provider execution did not complete successfully."),
        "source-drift": ("source-drift", "high", "The source workspace changed while the staged run was active."),
        "invalid-stage": (
            "invalid-stage",
            "high",
            "The staged workspace or generated change set failed strict validation.",
        ),
        "unsupported-binary": (
            "unsupported-binary",
            "medium",
            "The staged change set contains binary or non-UTF-8 files that cannot be emitted as a text patch.",
        ),
        "unsupported-metadata": (
            "unsupported-metadata",
            "medium",
            "The staged change set contains executable-bit changes that cannot be represented by the text patch contract.",
        ),
        "no-changes": ("no-changes", "low", "Claude completed without producing staged file changes."),
        "verification-failed": (
            "verification-failed",
            "medium",
            "The deterministic staged verifier exited nonzero.",
        ),
        "verification-timed-out": (
            "verification-timed-out",
            "medium",
            "The deterministic staged verifier exceeded its timeout.",
        ),
        "verifier-mutated-stage": (
            "verifier-mutated-stage",
            "high",
            "The verifier changed tracked staged files, so authorship and patch evidence are ambiguous.",
        ),
        "patch-failed": ("patch-failed", "high", "The validated staged patch could not be persisted."),
    }
    if status == "success":
        return []
    incident_id, severity, description = mapping[status]
    detail = source_scan_error or stage_scan_error or change_error or patch_error
    if detail:
        description += " %s" % redact_text(detail)
    return [{"id": incident_id, "severity": severity, "description": description}]


def _prepare_verifier(check_command: List[str], stage: Path) -> Dict:
    step = {"id": "claude-staged-check", "kind": "shell", "risk": "low", "command": check_command}
    verifier_policy = RuntimePolicy(allow_writes=True)
    user_argv = enforce_shell_policy(step, verifier_policy).argv
    stage_path = str(Path(stage).resolve())
    system = platform.system()
    if system == "Darwin":
        sandbox_path = Path("/usr/bin/sandbox-exec")
        if not sandbox_path.is_file():
            raise ValidationError("strict staged verification requires /usr/bin/sandbox-exec on macOS")
        command = [
            str(sandbox_path),
            "-D",
            "STAGE=%s" % stage_path,
            "-D",
            "USER_HOME=%s" % Path.home().resolve(),
            "-p",
            _MACOS_VERIFIER_PROFILE,
            *user_argv,
        ]
        sandbox = "macos-seatbelt"
        visible_stage = stage_path
    elif system == "Linux":
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise ValidationError("strict staged verification requires bubblewrap on Linux")
        command = [
            bwrap,
            "--die-with-parent",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/root",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            "/workspace",
            "--bind",
            stage_path,
            "/workspace",
            "--chdir",
            "/workspace",
            "--",
            *user_argv,
        ]
        sandbox = "linux-bwrap"
        visible_stage = "/workspace"
    else:
        raise ValidationError("strict staged verification is not supported on %s" % (system or "this host"))
    return {
        "user_argv": user_argv,
        "command": command,
        "sandbox": sandbox,
        "env": _sanitized_verifier_env(visible_stage),
    }


def _run_verifier(verifier: Dict, stage: Path, *, timeout_seconds: int, output_limit_bytes: int) -> Dict:
    _prepare_verifier_directories(stage)
    started = time.time()
    try:
        result = run_process(
            verifier["command"],
            cwd=stage,
            timeout=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            env=verifier["env"],
        )
    except OSError as exc:
        result = ProcessResult(
            returncode=127,
            stdout="",
            stderr="staged verifier launch failed: %s" % exc.__class__.__name__,
        )
    duration_ms = max(0, int((time.time() - started) * 1000))
    status = "timed-out" if result.timed_out else "passed" if result.returncode == 0 else "failed"
    return _verification_record(verifier, result, status=status, duration_ms=duration_ms)


def _skipped_verification(status: str, verifier: Dict) -> Dict:
    return {
        "configured": True,
        "status": status,
        "returncode": None,
        "timed_out": False,
        "duration_ms": 0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
        "command_sha256": _argv_sha256(verifier["user_argv"]),
        "argv_count": len(verifier["user_argv"]),
        "sandbox": verifier["sandbox"],
        "environment_sanitized": True,
        "network_isolated": True,
        "write_scope": CLAUDE_STAGED_VERIFIER_WRITE_SCOPE,
    }


def _verification_record(verifier: Dict, result: ProcessResult, *, status: str, duration_ms: int) -> Dict:
    return {
        "configured": True,
        "status": status,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "duration_ms": duration_ms,
        "stdout_bytes": len(result.stdout.encode("utf-8")),
        "stderr_bytes": len(result.stderr.encode("utf-8")),
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "stdout_excerpt": _bounded_verifier_excerpt(result.stdout),
        "stderr_excerpt": _bounded_verifier_excerpt(result.stderr),
        "command_sha256": _argv_sha256(verifier["user_argv"]),
        "argv_count": len(verifier["user_argv"]),
        "sandbox": verifier["sandbox"],
        "environment_sanitized": True,
        "network_isolated": True,
        "write_scope": CLAUDE_STAGED_VERIFIER_WRITE_SCOPE,
    }


def _bounded_verifier_excerpt(value: str) -> str:
    redacted = redact_text(value)
    if len(redacted) <= MAX_VERIFIER_EXCERPT_CHARS:
        return redacted
    retained_chars = MAX_VERIFIER_EXCERPT_CHARS - len(VERIFIER_EXCERPT_OMISSION)
    prefix_chars = retained_chars // 2
    suffix_chars = retained_chars - prefix_chars
    return redacted[:prefix_chars] + VERIFIER_EXCERPT_OMISSION + redacted[-suffix_chars:]


def _prepare_verifier_directories(stage: Path) -> None:
    for name in sorted(VERIFIER_RUNTIME_DIRECTORY_NAMES):
        directory_fd = ensure_dir_no_follow(Path(stage) / name, "staged verifier directory")
        os.close(directory_fd)


def _sanitized_verifier_env(visible_stage: str) -> Dict[str, str]:
    return {
        "HOME": "%s/.conductor-verifier-home" % visible_stage,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": "%s/.conductor-verifier-tmp" % visible_stage,
    }


def _validate_verification(value: Dict, source: str) -> None:
    if value.get("configured") is not True:
        raise ValidationError("%s staged verification must be configured" % source)
    if value.get("status") not in CLAUDE_STAGED_VERIFICATION_STATUSES:
        raise ValidationError("%s staged verification status is invalid" % source)
    if value.get("returncode") is not None and (
        isinstance(value["returncode"], bool) or not isinstance(value["returncode"], int)
    ):
        raise ValidationError("%s staged verification returncode must be integer or null" % source)
    for field in ["timed_out", "stdout_truncated", "stderr_truncated"]:
        if not isinstance(value.get(field), bool):
            raise ValidationError("%s staged verification %s must be boolean" % (source, field))
    for field in ["duration_ms", "stdout_bytes", "stderr_bytes"]:
        _bounded_int(value.get(field), "%s staged verification %s" % (source, field), 0, 10**12)
    _bounded_int(value.get("argv_count"), "%s staged verification argv_count" % source, 1, 1024)
    for field in ["stdout_excerpt", "stderr_excerpt"]:
        if not isinstance(value.get(field), str) or len(value[field]) > MAX_VERIFIER_EXCERPT_CHARS:
            raise ValidationError("%s staged verification %s is invalid" % (source, field))
    _sha256(value.get("command_sha256"), "%s staged verification command_sha256" % source)
    if value.get("sandbox") not in CLAUDE_STAGED_VERIFIER_SANDBOXES:
        raise ValidationError("%s staged verification sandbox is invalid" % source)
    if value.get("environment_sanitized") is not True or value.get("network_isolated") is not True:
        raise ValidationError("%s staged verification isolation metadata is invalid" % source)
    if value.get("write_scope") != CLAUDE_STAGED_VERIFIER_WRITE_SCOPE:
        raise ValidationError("%s staged verification write scope is invalid" % source)
    skipped = value["status"].startswith("skipped-")
    if skipped and (
        value["returncode"] is not None
        or value["duration_ms"] != 0
        or value["timed_out"]
        or value["stdout_bytes"] != 0
        or value["stderr_bytes"] != 0
        or value["stdout_truncated"]
        or value["stderr_truncated"]
        or value["stdout_excerpt"]
        or value["stderr_excerpt"]
    ):
        raise ValidationError("%s skipped staged verification metadata is inconsistent" % source)
    if value["status"] == "passed" and (value["returncode"] != 0 or value["timed_out"]):
        raise ValidationError("%s passed staged verification metadata is inconsistent" % source)
    if value["status"] == "failed" and (
        value["returncode"] is None or value["returncode"] == 0 or value["timed_out"]
    ):
        raise ValidationError("%s failed staged verification metadata is inconsistent" % source)
    if value["status"] == "timed-out" and not value["timed_out"]:
        raise ValidationError("%s timed-out staged verification metadata is inconsistent" % source)


def _validate_limits(
    task: Dict,
    *,
    max_budget_usd: float,
    max_turns: int,
    timeout_seconds: int,
    check_timeout_seconds: int,
    output_limit_bytes: int,
    check_output_limit_bytes: int,
) -> float:
    if isinstance(max_budget_usd, bool) or not isinstance(max_budget_usd, (int, float)):
        raise ValidationError("max_budget_usd must be a finite positive number")
    budget = float(max_budget_usd)
    if not math.isfinite(budget) or budget <= 0:
        raise ValidationError("max_budget_usd must be a finite positive number")
    fixture_budget = task["budget"].get("max_cost_usd")
    if not isinstance(fixture_budget, (int, float)) or isinstance(fixture_budget, bool) or fixture_budget <= 0:
        raise ValidationError("parity task %s does not authorize paid live-tool cost" % task["id"])
    if budget > float(fixture_budget):
        raise ValidationError("max_budget_usd exceeds parity task %s cap" % task["id"])
    _bounded_int(max_turns, "max_turns", 1, MAX_CLAUDE_TURNS)
    _bounded_int(timeout_seconds, "timeout_seconds", 1, min(MAX_TIMEOUT_SECONDS, task["budget"]["max_minutes"] * 60))
    _bounded_int(check_timeout_seconds, "check_timeout_seconds", 1, MAX_VERIFIER_TIMEOUT_SECONDS)
    _bounded_int(output_limit_bytes, "output_limit_bytes", 1, 16 * DEFAULT_OUTPUT_LIMIT_BYTES)
    _bounded_int(check_output_limit_bytes, "check_output_limit_bytes", 1, DEFAULT_OUTPUT_LIMIT_BYTES)
    return budget


def _require_staged_policy(policy: RuntimePolicy) -> None:
    if not policy.allow_agent:
        raise PolicyError("Claude staged parity requires --allow-agent")
    if not policy.allow_network:
        raise PolicyError("Claude staged parity requires --allow-network")
    if not policy.allow_writes:
        raise PolicyError("Claude staged parity requires --allow-writes")
    for approval in [CLAUDE_PAID_RUN_APPROVAL, CLAUDE_STAGED_WRITE_APPROVAL]:
        if not policy.has_approval(approval):
            raise PolicyError("Claude staged parity requires --approve %s" % approval)


def _parity_task(parity_tasks: Dict, task_id: str) -> Dict:
    if not isinstance(task_id, str) or not SAFE_ID.match(task_id):
        raise ValidationError("Claude staged task id must be a safe identifier")
    matches = [task for task in parity_tasks["tasks"] if task["id"] == task_id]
    if len(matches) != 1:
        raise ValidationError("Claude staged task does not exist: %s" % task_id)
    return matches[0]


def _staged_prompt(
    task_prompt: str,
    *,
    task_execution_guidance: str,
    task_static_audit_guidance: str,
    workspace_context_packet: Optional[str],
) -> str:
    static_audit_guidance = task_static_audit_guidance
    if not static_audit_guidance and not task_execution_guidance:
        static_audit_guidance = PUBLIC_CONTRACT_CHECKLIST_GUIDANCE
    prompt = (
        task_prompt
        + "\n\nConductor staged-workspace contract:\n"
        + "- Modify only required files in the staged workspace.\n"
        + "- No shell, network, commits, pushes, dependencies, or external paths.\n"
        + "- Use only available read and file-edit tools. A deterministic verifier follows; do not simulate checks.\n"
    )
    prompt += render_small_workspace_context_block(workspace_context_packet)
    if static_audit_guidance:
        prompt += "\nConductor pre-write contract checklist:\n" + static_audit_guidance + "\n"
    if task_execution_guidance:
        prompt += "\nConductor task-type policy:\n" + task_execution_guidance + "\n"
    return prompt + STAGED_POST_WRITE_FINALIZATION_GUIDANCE + "\n"


def _empty_changes(validation_error: Optional[str] = None) -> Dict:
    empty_patch = ""
    return {
        "added": [],
        "modified": [],
        "deleted": [],
        "binary": [],
        "mode_changed": [],
        "unpatchable": [],
        "change_count": 0,
        "patch_text": empty_patch,
        "patch_bytes": 0,
        "patch_sha256": hashlib.sha256(empty_patch.encode("utf-8")).hexdigest(),
        "validation_error": validation_error,
    }


def _strict_object(value, fields: set, label: str) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % label)
    _exact_fields(value, fields, label)
    return value


def _exact_fields(value: Dict, fields: set, label: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown or missing:
        raise ValidationError("%s fields are invalid" % label)


def _sha256(value, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError("%s must be a SHA-256 hex string" % label)
    return value


def _path_list(value, label: str) -> List[str]:
    if not isinstance(value, list) or len(value) > MAX_STAGED_CHANGES:
        raise ValidationError("%s must be a bounded path list" % label)
    if not all(_safe_relative_evidence_path(path) for path in value):
        raise ValidationError("%s contains invalid paths" % label)
    if value != sorted(set(value)):
        raise ValidationError("%s must be sorted and unique" % label)
    return value


def _safe_relative_evidence_path(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    if value.startswith("/") or any(char in value for char in "\x00\r\n"):
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _safe_artifact_name(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value not in {".", ".."}
        and not any(char in value for char in "/\x00\r\n")
    )


def _optional_error(value, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValidationError("%s must be a bounded non-empty string or null" % label)


def _bounded_error_text(value) -> str:
    text = redact_text(str(value))
    if not text:
        text = value.__class__.__name__ if isinstance(value, BaseException) else "validation error"
    return text[:4096]


def _bounded_int(value, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValidationError("%s must be an integer between %d and %d" % (label, minimum, maximum))
    return value


def _argv_sha256(argv: List[str]) -> str:
    return hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValidationError("Claude staged evidence JSON contains duplicate key: %s" % key)
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValidationError("Claude staged evidence JSON contains invalid numeric constant: %s" % value)
