import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .agent_team_turn_completion import (
    agent_team_turn_telemetry_payload,
    provider_telemetry_from_agent_team_turn_telemetry,
    validate_agent_team_turn_telemetry,
)
from .codex_checkpoint import (
    CODEX_STEP_CHECKPOINT_SCHEMA_V1,
    MAX_CODEX_STEP_RESUMES,
    validate_codex_step_checkpoint,
)
from .codex_config import MIN_CODEX_RUNTIME_TOKEN_CAP
from .errors import ValidationError
from .provider_telemetry import MAX_PROVIDER_TOKENS, ProviderTelemetry
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    require_no_path_escape,
    unlink_regular_file_no_follow,
    write_new_text_file_no_follow,
)
from .staged_workspace import MAX_STAGED_FILES, MAX_STAGED_TOTAL_BYTES


CODEX_STEP_TERMINAL_SCHEMA_V1 = "conductor.codex_step_terminal.v1"
CODEX_STEP_TERMINAL_SCHEMA = "conductor.codex_step_terminal.v2"
MAX_CODEX_STEP_TERMINAL_BYTES = 256 * 1024
MAX_CODEX_STEP_TERMINAL_EVENTS = 64
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_FIELDS_V1 = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "created_at_utc",
    "started_at_utc",
    "terminal_at_utc",
    "mode",
    "resume_count",
    "max_tokens",
    "sandbox",
    "model",
    "effort",
    "prompt_sha256",
    "output",
    "output_raw_sha256",
    "output_redacted_sha256",
    "session_id",
    "session_id_sha256",
    "checkpoint_sha256",
    "telemetry",
    "workspace_path_sha256",
    "workspace_mode",
    "workspace_result_sha256",
    "workspace_excluded_sha256",
    "workspace_file_count",
    "workspace_total_bytes",
    "terminal_sha256",
}
TERMINAL_FIELDS = TERMINAL_FIELDS_V1 | {
    "invocation_base_prompt_sha256",
    "invocation_effective_prompt_sha256",
    "lifecycle_context_receipt_sha256",
}


def build_codex_step_terminal(
    *,
    step: Dict,
    workflow_fingerprint: str,
    checkpoint: Dict,
    checkpoint_sha256: str,
    started_at_utc: str,
    terminal_at_utc: str,
    max_tokens: Optional[int],
    effort: Optional[str],
    output_raw_sha256: str,
    output_redacted_sha256: str,
    telemetry: ProviderTelemetry,
    workspace_path_sha256: str,
    workspace_result_sha256: Optional[str] = None,
    workspace_excluded_sha256: Optional[str] = None,
    workspace_file_count: Optional[int] = None,
    workspace_total_bytes: Optional[int] = None,
) -> Dict:
    validate_codex_step_checkpoint(checkpoint)
    if checkpoint["status"] != "active":
        raise ValidationError("Codex step terminal requires an active checkpoint")
    write_step = step.get("sandbox", "read-only") == "workspace-write"
    current_checkpoint = checkpoint["schema"] != CODEX_STEP_CHECKPOINT_SCHEMA_V1
    terminal = {
        "schema": (
            CODEX_STEP_TERMINAL_SCHEMA
            if current_checkpoint
            else CODEX_STEP_TERMINAL_SCHEMA_V1
        ),
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "created_at_utc": terminal_at_utc,
        "started_at_utc": started_at_utc,
        "terminal_at_utc": terminal_at_utc,
        "mode": checkpoint["mode"],
        "resume_count": checkpoint["resume_count"],
        "max_tokens": max_tokens,
        "sandbox": step.get("sandbox", "read-only"),
        "model": step.get("model"),
        "effort": effort,
        "prompt_sha256": checkpoint["prompt_sha256"],
        "output": checkpoint["output"],
        "output_raw_sha256": output_raw_sha256,
        "output_redacted_sha256": output_redacted_sha256,
        "session_id": checkpoint["session_id"],
        "session_id_sha256": _sha256_text(checkpoint["session_id"]),
        "checkpoint_sha256": checkpoint_sha256,
        "telemetry": agent_team_turn_telemetry_payload(telemetry),
        "workspace_path_sha256": workspace_path_sha256,
        "workspace_mode": "direct-write" if write_step else "read-only",
        "workspace_result_sha256": workspace_result_sha256 if write_step else None,
        "workspace_excluded_sha256": workspace_excluded_sha256 if write_step else None,
        "workspace_file_count": workspace_file_count if write_step else None,
        "workspace_total_bytes": workspace_total_bytes if write_step else None,
    }
    if current_checkpoint:
        terminal.update(
            {
                "invocation_base_prompt_sha256": checkpoint[
                    "invocation_base_prompt_sha256"
                ],
                "invocation_effective_prompt_sha256": checkpoint[
                    "invocation_effective_prompt_sha256"
                ],
                "lifecycle_context_receipt_sha256": checkpoint[
                    "lifecycle_context_receipt_sha256"
                ],
            }
        )
    terminal["terminal_sha256"] = _terminal_sha256(terminal)
    validate_codex_step_terminal(
        terminal,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        checkpoint=checkpoint,
        checkpoint_sha256=checkpoint_sha256,
    )
    return terminal


def validate_codex_step_terminal(
    terminal: Dict,
    *,
    step: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
    checkpoint: Optional[Dict] = None,
    checkpoint_sha256: Optional[str] = None,
    workspace_path_sha256: Optional[str] = None,
) -> None:
    if not isinstance(terminal, dict):
        raise ValidationError("Codex step terminal has invalid fields")
    schema = terminal.get("schema")
    expected_fields = (
        TERMINAL_FIELDS
        if schema == CODEX_STEP_TERMINAL_SCHEMA
        else TERMINAL_FIELDS_V1
        if schema == CODEX_STEP_TERMINAL_SCHEMA_V1
        else None
    )
    if expected_fields is None:
        raise ValidationError("Codex step terminal schema is invalid")
    if set(terminal) != expected_fields:
        raise ValidationError("Codex step terminal has invalid fields")
    _validate_id(terminal.get("step_id"), "Codex step terminal step_id")
    for field in (
        "workflow_fingerprint",
        "prompt_sha256",
        "output_raw_sha256",
        "output_redacted_sha256",
        "session_id_sha256",
        "checkpoint_sha256",
        "workspace_path_sha256",
        "terminal_sha256",
    ):
        _validate_sha256(terminal.get(field), "Codex step terminal %s" % field)
    if schema == CODEX_STEP_TERMINAL_SCHEMA:
        for field in (
            "invocation_base_prompt_sha256",
            "invocation_effective_prompt_sha256",
        ):
            _validate_sha256(terminal.get(field), "Codex step terminal %s" % field)
        receipt_sha256 = terminal.get("lifecycle_context_receipt_sha256")
        if receipt_sha256 is None:
            if (
                terminal["invocation_effective_prompt_sha256"]
                != terminal["invocation_base_prompt_sha256"]
            ):
                raise ValidationError(
                    "Codex step terminal invocation prompt changed without a context receipt"
                )
        elif receipt_sha256 is not None:
            _validate_sha256(
                receipt_sha256,
                "Codex step terminal lifecycle context receipt",
            )
            if (
                terminal["invocation_effective_prompt_sha256"]
                == terminal["invocation_base_prompt_sha256"]
            ):
                raise ValidationError(
                    "Codex step terminal context receipt did not change the prompt"
                )
    if (
        workflow_fingerprint is not None
        and terminal["workflow_fingerprint"] != workflow_fingerprint
    ):
        raise ValidationError("Codex step terminal workflow fingerprint changed")
    for field in ("created_at_utc", "started_at_utc", "terminal_at_utc"):
        _validate_timestamp(terminal.get(field), "Codex step terminal %s" % field)
    if terminal["created_at_utc"] != terminal["terminal_at_utc"]:
        raise ValidationError("Codex step terminal creation timestamp changed")
    if _timestamp_value(terminal["started_at_utc"]) > _timestamp_value(
        terminal["terminal_at_utc"]
    ):
        raise ValidationError("Codex step terminal timestamps are inconsistent")
    if terminal.get("mode") not in {"started", "resumed"}:
        raise ValidationError("Codex step terminal mode is invalid")
    _validate_int(
        terminal.get("resume_count"),
        0,
        MAX_CODEX_STEP_RESUMES,
        "resume_count",
    )
    max_tokens = terminal.get("max_tokens")
    if max_tokens is not None:
        _validate_int(
            max_tokens,
            MIN_CODEX_RUNTIME_TOKEN_CAP,
            MAX_PROVIDER_TOKENS,
            "max_tokens",
        )
    if terminal.get("sandbox") not in {"read-only", "workspace-write"}:
        raise ValidationError("Codex step terminal sandbox is invalid")
    for field in ("model", "effort"):
        value = terminal.get(field)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 200
        ):
            raise ValidationError("Codex step terminal %s is invalid" % field)
    output = terminal.get("output")
    if not isinstance(output, str) or not output or len(output) > 500:
        raise ValidationError("Codex step terminal output is invalid")
    require_no_path_escape(output)
    _validate_uuid(terminal.get("session_id"), "Codex step terminal session_id")
    if _sha256_text(terminal["session_id"]) != terminal["session_id_sha256"]:
        raise ValidationError("Codex step terminal session hash changed")
    validate_agent_team_turn_telemetry(terminal.get("telemetry"))
    if len(terminal["telemetry"]["events"]) > MAX_CODEX_STEP_TERMINAL_EVENTS:
        raise ValidationError("Codex step terminal has too many provider events")
    _validate_terminal_telemetry(terminal)
    if (
        workspace_path_sha256 is not None
        and terminal["workspace_path_sha256"] != workspace_path_sha256
    ):
        raise ValidationError("Codex step terminal workspace path changed")
    _validate_workspace_evidence(terminal)
    if terminal["terminal_sha256"] != _terminal_sha256(terminal):
        raise ValidationError("Codex step terminal hash is invalid")
    if step is not None:
        if step.get("kind") != "codex_exec" or terminal["step_id"] != step.get("id"):
            raise ValidationError("Codex step terminal step binding changed")
        if terminal["sandbox"] != step.get("sandbox", "read-only"):
            raise ValidationError("Codex step terminal sandbox binding changed")
        if terminal["model"] != step.get("model"):
            raise ValidationError("Codex step terminal model binding changed")
        expected_output = step.get("capture", "%s.md" % step["id"])
        if terminal["output"] != expected_output:
            raise ValidationError("Codex step terminal output binding changed")
        expected_mode = (
            "direct-write"
            if step.get("sandbox", "read-only") == "workspace-write"
            else "read-only"
        )
        if terminal["workspace_mode"] != expected_mode:
            raise ValidationError("Codex step terminal workspace authority changed")
    if checkpoint is not None:
        _validate_checkpoint_binding(terminal, checkpoint, checkpoint_sha256)


def codex_step_terminal_path(run, step_id: str) -> Path:
    relative = _codex_step_terminal_relative(step_id)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "Codex step terminal")
    return path


def codex_step_terminal_path_from_run_dir(run_dir: Path, step_id: str) -> Path:
    relative = _codex_step_terminal_relative(step_id)
    path = Path(run_dir) / "artifacts" / relative
    reject_symlink_path(path, "Codex step terminal")
    return path


def _codex_step_terminal_relative(step_id: str) -> str:
    _validate_id(step_id, "Codex step terminal step_id")
    name = hashlib.sha256(step_id.encode("utf-8")).hexdigest() + ".json"
    relative = ".codex-step-terminals/%s" % name
    require_no_path_escape(relative)
    return relative


def load_codex_step_terminal(path: Path, **validation) -> Dict:
    reject_symlink_path(path, "Codex step terminal")
    text = read_regular_text_file_no_follow(
        path,
        "Codex step terminal",
        MAX_CODEX_STEP_TERMINAL_BYTES,
    )
    try:
        terminal = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "Codex step terminal is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_codex_step_terminal(terminal, **validation)
    return terminal


def write_codex_step_terminal(run, terminal: Dict) -> Path:
    validate_codex_step_terminal(terminal)
    path = codex_step_terminal_path(run, terminal["step_id"])
    parent_fd = ensure_dir_no_follow(path.parent, "Codex step terminal parent")
    os.close(parent_fd)
    text = json.dumps(terminal, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_CODEX_STEP_TERMINAL_BYTES:
        raise ValidationError("Codex step terminal exceeds its byte limit")
    write_new_text_file_no_follow(path, "Codex step terminal", text)
    return path


def remove_codex_step_terminal(path: Path) -> None:
    unlink_regular_file_no_follow(path, "Codex step terminal")


def provider_telemetry_from_codex_step_terminal(terminal: Dict) -> ProviderTelemetry:
    validate_codex_step_terminal(terminal)
    return provider_telemetry_from_agent_team_turn_telemetry(terminal["telemetry"])


def codex_step_terminal_summary(terminal: Dict) -> Dict:
    validate_codex_step_terminal(terminal)
    telemetry = terminal["telemetry"]
    return {
        "schema": terminal["schema"],
        "terminal_sha256": terminal["terminal_sha256"],
        "step_id": terminal["step_id"],
        "mode": terminal["mode"],
        "resume_count": terminal["resume_count"],
        "max_tokens": terminal["max_tokens"],
        "sandbox": terminal["sandbox"],
        "model": terminal["model"],
        "effort": terminal["effort"],
        "prompt_sha256": terminal["prompt_sha256"],
        "invocation_base_prompt_sha256": terminal.get(
            "invocation_base_prompt_sha256"
        ),
        "invocation_effective_prompt_sha256": terminal.get(
            "invocation_effective_prompt_sha256"
        ),
        "lifecycle_context_receipt_sha256": terminal.get(
            "lifecycle_context_receipt_sha256"
        ),
        "output_redacted_sha256": terminal["output_redacted_sha256"],
        "session_id_sha256": terminal["session_id_sha256"],
        "checkpoint_sha256": terminal["checkpoint_sha256"],
        "workspace_path_sha256": terminal["workspace_path_sha256"],
        "workspace_mode": terminal["workspace_mode"],
        "workspace_result_sha256": terminal["workspace_result_sha256"],
        "workspace_excluded_sha256": terminal["workspace_excluded_sha256"],
        "workspace_file_count": terminal["workspace_file_count"],
        "workspace_total_bytes": terminal["workspace_total_bytes"],
        "telemetry_event_count": len(telemetry["events"]),
        "input_tokens": telemetry["input_tokens"],
        "output_tokens": telemetry["output_tokens"],
        "total_tokens": telemetry["total_tokens"],
        "cost_usd": telemetry["cost_usd"],
        "status": "provider-terminal-recovery-pending",
        "provider_replay_required": False,
    }


def _validate_checkpoint_binding(
    terminal: Dict,
    checkpoint: Dict,
    checkpoint_sha256: Optional[str],
) -> None:
    validate_codex_step_checkpoint(checkpoint)
    for terminal_field, checkpoint_field in (
        ("step_id", "step_id"),
        ("workflow_fingerprint", "workflow_fingerprint"),
        ("session_id", "session_id"),
        ("prompt_sha256", "prompt_sha256"),
        ("sandbox", "sandbox"),
        ("model", "model"),
        ("output", "output"),
        ("mode", "mode"),
        ("resume_count", "resume_count"),
    ):
        if terminal[terminal_field] != checkpoint[checkpoint_field]:
            raise ValidationError(
                "Codex step terminal checkpoint %s binding changed" % checkpoint_field
            )
    if terminal["schema"] == CODEX_STEP_TERMINAL_SCHEMA:
        for field in (
            "invocation_base_prompt_sha256",
            "invocation_effective_prompt_sha256",
            "lifecycle_context_receipt_sha256",
        ):
            if terminal[field] != checkpoint.get(field):
                raise ValidationError(
                    "Codex step terminal checkpoint %s binding changed" % field
                )
    if checkpoint["status"] == "active":
        _validate_sha256(checkpoint_sha256, "Codex step terminal checkpoint hash")
        if terminal["checkpoint_sha256"] != checkpoint_sha256:
            raise ValidationError("Codex step terminal active checkpoint changed")
    elif checkpoint["status"] == "completed":
        if checkpoint["output_sha256"] != terminal["output_redacted_sha256"]:
            raise ValidationError("Codex step terminal completed checkpoint output changed")
    else:
        raise ValidationError("Codex step terminal checkpoint is failed")


def _validate_workspace_evidence(terminal: Dict) -> None:
    mode = terminal.get("workspace_mode")
    fields = (
        "workspace_result_sha256",
        "workspace_excluded_sha256",
        "workspace_file_count",
        "workspace_total_bytes",
    )
    if mode == "read-only":
        if any(terminal[field] is not None for field in fields):
            raise ValidationError("read-only Codex step terminal has workspace evidence")
        return
    if mode != "direct-write":
        raise ValidationError("Codex step terminal workspace mode is invalid")
    _validate_sha256(
        terminal.get("workspace_result_sha256"),
        "Codex step terminal workspace result",
    )
    _validate_sha256(
        terminal.get("workspace_excluded_sha256"),
        "Codex step terminal workspace exclusions",
    )
    _validate_int(
        terminal.get("workspace_file_count"),
        0,
        MAX_STAGED_FILES,
        "workspace_file_count",
    )
    _validate_int(
        terminal.get("workspace_total_bytes"),
        0,
        MAX_STAGED_TOTAL_BYTES,
        "workspace_total_bytes",
    )


def _validate_terminal_telemetry(terminal: Dict) -> None:
    events = terminal["telemetry"]["events"]
    if any(event.get("provider") != "codex" for event in events):
        raise ValidationError("Codex step terminal provider changed")
    sessions = [
        event.get("session_id")
        for event in events
        if str(event.get("event", "")).lower() == "thread.started"
    ]
    if not sessions or set(sessions) != {terminal["session_id"]}:
        raise ValidationError("Codex step terminal session evidence changed")
    terminal_events = [
        event
        for event in events
        if str(event.get("event", "")).lower() in {"turn.completed", "turn_completed"}
    ]
    if len(terminal_events) != 1 or events[-1] is not terminal_events[0]:
        raise ValidationError("Codex step terminal event evidence is invalid")


def _terminal_sha256(terminal: Dict) -> str:
    value = dict(terminal)
    value.pop("terminal_sha256", None)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError("%s is invalid" % label)


def _validate_uuid(value, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("%s is invalid" % label)
    if str(parsed) != value:
        raise ValidationError("%s is invalid" % label)


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _validate_int(value, minimum: int, maximum: int, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError("Codex step terminal %s is invalid" % label)


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)
