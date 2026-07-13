import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .agent_team import (
    MAX_AGENT_TEAM_GENERATION,
    MAX_AGENT_TEAM_MESSAGES,
    MAX_AGENT_TEAM_ROUNDS,
    MAX_AGENT_TEAM_TASKS,
    MAX_AGENT_TEAM_TURNS,
    validate_agent_team_state,
)
from .agent_team_merge import (
    MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES,
    agent_team_state_sha256,
)
from .agent_team_quality_retry import MAX_AGENT_TEAM_QUALITY_RETRIES
from .agent_team_turn_completion import (
    agent_team_turn_telemetry_payload,
    provider_telemetry_from_agent_team_turn_telemetry,
    validate_agent_team_turn_telemetry,
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
from .staged_workspace import workspace_snapshot_from_manifest


AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1 = "conductor.agent_team_turn_terminal.v1"
AGENT_TEAM_TURN_TERMINAL_SCHEMA = "conductor.agent_team_turn_terminal.v2"
MAX_AGENT_TEAM_TURN_TERMINAL_BYTES = MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_FIELDS_V1 = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "round",
    "member_id",
    "task_id",
    "quality_retry_index",
    "created_at_utc",
    "started_at_utc",
    "terminal_at_utc",
    "max_tokens",
    "output",
    "output_raw_sha256",
    "output_redacted_sha256",
    "session_id",
    "session_id_sha256",
    "launch_state_sha256",
    "launch_turn_count",
    "launch_task_count",
    "launch_message_count",
    "launch_authorized_tokens",
    "telemetry",
    "workspace_mode",
    "workspace_relative",
    "workspace_base_manifest",
    "workspace_base_sha256",
    "workspace_result_sha256",
    "terminal_sha256",
}
TERMINAL_FIELDS = TERMINAL_FIELDS_V1 | {
    "session_mode",
    "base_prompt_sha256",
    "effective_prompt_sha256",
    "lifecycle_context_receipt_sha256",
}


def build_agent_team_turn_terminal(
    *,
    step: Dict,
    state: Dict,
    workflow_fingerprint: str,
    member_id: str,
    task_id: str,
    quality_retry_index: int,
    output: str,
    output_raw_sha256: str,
    output_redacted_sha256: str,
    session_id: str,
    started_at_utc: str,
    terminal_at_utc: str,
    max_tokens: int,
    telemetry: ProviderTelemetry,
    session_mode: str,
    base_prompt_sha256: str,
    effective_prompt_sha256: str,
    lifecycle_context_receipt_sha256: Optional[str],
    workspace_relative: Optional[str] = None,
    workspace_base_manifest: Optional[Dict] = None,
    workspace_result_sha256: Optional[str] = None,
) -> Dict:
    validate_agent_team_state(
        state,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=state.get("generation"),
    )
    write_team = step.get("sandbox", "read-only") == "workspace-write"
    workspace_mode = "isolated-write" if write_team else "read-only"
    workspace_base_sha256 = None
    if write_team:
        workspace_base_sha256 = workspace_snapshot_from_manifest(
            workspace_base_manifest
        ).tracked_fingerprint_sha256
    terminal = {
        "schema": AGENT_TEAM_TURN_TERMINAL_SCHEMA,
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "generation": state["generation"],
        "round": state["round"],
        "member_id": member_id,
        "task_id": task_id,
        "quality_retry_index": quality_retry_index,
        "created_at_utc": terminal_at_utc,
        "started_at_utc": started_at_utc,
        "terminal_at_utc": terminal_at_utc,
        "max_tokens": max_tokens,
        "output": output,
        "output_raw_sha256": output_raw_sha256,
        "output_redacted_sha256": output_redacted_sha256,
        "session_id": session_id,
        "session_id_sha256": _sha256_text(session_id),
        "launch_state_sha256": agent_team_state_sha256(state),
        "launch_turn_count": len(state["turns"]),
        "launch_task_count": len(state["tasks"]),
        "launch_message_count": len(state["messages"]),
        "launch_authorized_tokens": state["authorized_tokens"],
        "telemetry": agent_team_turn_telemetry_payload(telemetry),
        "session_mode": session_mode,
        "base_prompt_sha256": base_prompt_sha256,
        "effective_prompt_sha256": effective_prompt_sha256,
        "lifecycle_context_receipt_sha256": lifecycle_context_receipt_sha256,
        "workspace_mode": workspace_mode,
        "workspace_relative": workspace_relative if write_team else None,
        "workspace_base_manifest": (
            json.loads(json.dumps(workspace_base_manifest)) if write_team else None
        ),
        "workspace_base_sha256": workspace_base_sha256,
        "workspace_result_sha256": workspace_result_sha256 if write_team else None,
    }
    terminal["terminal_sha256"] = _terminal_sha256(terminal)
    validate_agent_team_turn_terminal(
        terminal,
        step=step,
        state=state,
        workflow_fingerprint=workflow_fingerprint,
    )
    return terminal


def validate_agent_team_turn_terminal(
    terminal: Dict,
    *,
    step: Optional[Dict] = None,
    state: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
) -> None:
    if not isinstance(terminal, dict):
        raise ValidationError("agent team turn terminal has invalid fields")
    schema = terminal.get("schema")
    expected_fields = (
        TERMINAL_FIELDS
        if schema == AGENT_TEAM_TURN_TERMINAL_SCHEMA
        else TERMINAL_FIELDS_V1
        if schema == AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1
        else None
    )
    if expected_fields is None:
        raise ValidationError("agent team turn terminal schema is invalid")
    if set(terminal) != expected_fields:
        raise ValidationError("agent team turn terminal has invalid fields")
    for field in ("step_id", "member_id", "task_id"):
        _validate_id(terminal.get(field), "agent team turn terminal %s" % field)
    _validate_sha256(
        terminal.get("workflow_fingerprint"),
        "agent team turn terminal workflow fingerprint",
    )
    if (
        workflow_fingerprint is not None
        and terminal["workflow_fingerprint"] != workflow_fingerprint
    ):
        raise ValidationError("agent team turn terminal workflow fingerprint changed")
    _validate_int(terminal.get("generation"), 0, MAX_AGENT_TEAM_GENERATION, "generation")
    _validate_int(terminal.get("round"), 1, MAX_AGENT_TEAM_ROUNDS, "round")
    _validate_int(
        terminal.get("quality_retry_index"),
        0,
        MAX_AGENT_TEAM_QUALITY_RETRIES,
        "quality_retry_index",
    )
    _validate_int(
        terminal.get("max_tokens"),
        MIN_CODEX_RUNTIME_TOKEN_CAP,
        MAX_PROVIDER_TOKENS,
        "max_tokens",
    )
    for field in ("created_at_utc", "started_at_utc", "terminal_at_utc"):
        _validate_timestamp(terminal.get(field), "agent team turn terminal %s" % field)
    if terminal["created_at_utc"] != terminal["terminal_at_utc"]:
        raise ValidationError("agent team turn terminal creation timestamp changed")
    if _timestamp_value(terminal["started_at_utc"]) > _timestamp_value(
        terminal["terminal_at_utc"]
    ):
        raise ValidationError("agent team turn terminal timestamps are inconsistent")
    output = terminal.get("output")
    if not isinstance(output, str) or not output:
        raise ValidationError("agent team turn terminal output is invalid")
    require_no_path_escape(output)
    for field in (
        "output_raw_sha256",
        "output_redacted_sha256",
        "session_id_sha256",
        "launch_state_sha256",
        "terminal_sha256",
    ):
        _validate_sha256(terminal.get(field), "agent team turn terminal %s" % field)
    if schema == AGENT_TEAM_TURN_TERMINAL_SCHEMA:
        if terminal.get("session_mode") not in {"new", "resume"}:
            raise ValidationError("agent team turn terminal session mode is invalid")
        _validate_sha256(
            terminal.get("base_prompt_sha256"),
            "agent team turn terminal base prompt",
        )
        _validate_sha256(
            terminal.get("effective_prompt_sha256"),
            "agent team turn terminal effective prompt",
        )
        receipt_sha256 = terminal.get("lifecycle_context_receipt_sha256")
        if receipt_sha256 is not None:
            _validate_sha256(
                receipt_sha256,
                "agent team turn terminal lifecycle context receipt",
            )
    _validate_uuid(terminal.get("session_id"), "agent team turn terminal session_id")
    if _sha256_text(terminal["session_id"]) != terminal["session_id_sha256"]:
        raise ValidationError("agent team turn terminal session hash changed")
    _validate_int(
        terminal.get("launch_turn_count"), 0, MAX_AGENT_TEAM_TURNS, "launch_turn_count"
    )
    _validate_int(
        terminal.get("launch_task_count"), 1, MAX_AGENT_TEAM_TASKS, "launch_task_count"
    )
    _validate_int(
        terminal.get("launch_message_count"),
        0,
        MAX_AGENT_TEAM_MESSAGES,
        "launch_message_count",
    )
    _validate_int(
        terminal.get("launch_authorized_tokens"),
        MIN_CODEX_RUNTIME_TOKEN_CAP,
        MAX_PROVIDER_TOKENS,
        "launch_authorized_tokens",
    )
    validate_agent_team_turn_telemetry(terminal.get("telemetry"))
    _validate_terminal_telemetry(terminal)
    workspace_mode = terminal.get("workspace_mode")
    if workspace_mode not in {"read-only", "isolated-write"}:
        raise ValidationError("agent team turn terminal workspace mode is invalid")
    if workspace_mode == "read-only":
        if any(
            terminal[field] is not None
            for field in (
                "workspace_relative",
                "workspace_base_manifest",
                "workspace_base_sha256",
                "workspace_result_sha256",
            )
        ):
            raise ValidationError("read-only agent team turn terminal has workspace evidence")
    else:
        workspace_relative = terminal.get("workspace_relative")
        if not isinstance(workspace_relative, str) or not workspace_relative:
            raise ValidationError("agent team turn terminal workspace path is invalid")
        require_no_path_escape(workspace_relative)
        workspace_base = workspace_snapshot_from_manifest(
            terminal.get("workspace_base_manifest")
        )
        _validate_sha256(
            terminal.get("workspace_base_sha256"),
            "agent team turn terminal workspace base hash",
        )
        _validate_sha256(
            terminal.get("workspace_result_sha256"),
            "agent team turn terminal workspace result hash",
        )
        if workspace_base.tracked_fingerprint_sha256 != terminal["workspace_base_sha256"]:
            raise ValidationError("agent team turn terminal workspace base changed")
    if terminal["terminal_sha256"] != _terminal_sha256(terminal):
        raise ValidationError("agent team turn terminal hash is invalid")
    if step is not None:
        if terminal["step_id"] != step.get("id"):
            raise ValidationError("agent team turn terminal step binding changed")
        expected_mode = (
            "isolated-write"
            if step.get("sandbox", "read-only") == "workspace-write"
            else "read-only"
        )
        if terminal["workspace_mode"] != expected_mode:
            raise ValidationError("agent team turn terminal workspace authority changed")
        expected_output = "%s/round-%03d/%s--%s.json" % (
            step["capture_dir"],
            terminal["round"],
            terminal["member_id"],
            terminal["task_id"],
        )
        if terminal["output"] != expected_output:
            raise ValidationError("agent team turn terminal output binding changed")
    if state is not None:
        validate_agent_team_state(
            state,
            step=step,
            workflow_fingerprint=terminal["workflow_fingerprint"],
            generation=terminal["generation"],
        )
        if agent_team_state_sha256(state) != terminal["launch_state_sha256"]:
            raise ValidationError("agent team turn terminal launch state changed")
        if state["round"] != terminal["round"]:
            raise ValidationError("agent team turn terminal launch round changed")
        member = next(
            (value for value in state["members"] if value["id"] == terminal["member_id"]),
            None,
        )
        task = next(
            (value for value in state["tasks"] if value["id"] == terminal["task_id"]),
            None,
        )
        if (
            member is None
            or task is None
            or member["current_task_id"] != terminal["task_id"]
            or task["claimed_by"] != terminal["member_id"]
        ):
            raise ValidationError("agent team turn terminal launch claim changed")
        if (
            len(state["turns"]) != terminal["launch_turn_count"]
            or len(state["tasks"]) != terminal["launch_task_count"]
            or len(state["messages"]) != terminal["launch_message_count"]
            or state["authorized_tokens"] != terminal["launch_authorized_tokens"]
        ):
            raise ValidationError("agent team turn terminal launch counts changed")


def agent_team_turn_terminal_path(run, step: Dict, member_id: str) -> Path:
    _validate_id(member_id, "agent team turn terminal member_id")
    relative = "%s/turn-terminals/%s.json" % (step["capture_dir"], member_id)
    require_no_path_escape(relative)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent team turn terminal")
    return path


def list_agent_team_turn_terminal_paths(run, step: Dict) -> List[Path]:
    relative = "%s/turn-terminals" % step["capture_dir"]
    require_no_path_escape(relative)
    directory = run.resolve_artifact_path(relative)
    reject_symlink_path(directory, "agent team turn terminal directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent team turn terminal directory is invalid")
    expected = {
        "%s.json" % member["id"]: agent_team_turn_terminal_path(
            run,
            step,
            member["id"],
        )
        for member in step["members"]
    }
    paths = []
    for path in sorted(directory.iterdir(), key=lambda value: value.name):
        reject_symlink_path(path, "agent team turn terminal")
        if path.name not in expected or path != expected[path.name] or not path.is_file():
            raise ValidationError("agent team turn terminal directory has an unknown entry")
        paths.append(path)
    return paths


def load_agent_team_turn_terminal(path: Path, **validation) -> Dict:
    reject_symlink_path(path, "agent team turn terminal")
    text = read_regular_text_file_no_follow(
        path,
        "agent team turn terminal",
        MAX_AGENT_TEAM_TURN_TERMINAL_BYTES,
    )
    try:
        terminal = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team turn terminal is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_turn_terminal(terminal, **validation)
    return terminal


def write_agent_team_turn_terminal(run, step: Dict, terminal: Dict) -> Path:
    validate_agent_team_turn_terminal(terminal, step=step)
    path = agent_team_turn_terminal_path(run, step, terminal["member_id"])
    parent_fd = ensure_dir_no_follow(path.parent, "agent team turn terminal parent")
    os.close(parent_fd)
    text = json.dumps(terminal, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_TURN_TERMINAL_BYTES:
        raise ValidationError("agent team turn terminal exceeds its byte limit")
    write_new_text_file_no_follow(path, "agent team turn terminal", text)
    return path


def remove_agent_team_turn_terminal(path: Path) -> None:
    unlink_regular_file_no_follow(path, "agent team turn terminal")


def provider_telemetry_from_turn_terminal(terminal: Dict) -> ProviderTelemetry:
    validate_agent_team_turn_terminal(terminal)
    return provider_telemetry_from_agent_team_turn_telemetry(terminal["telemetry"])


def agent_team_turn_terminal_summary(terminal: Dict) -> Dict:
    validate_agent_team_turn_terminal(terminal)
    telemetry = terminal["telemetry"]
    return {
        "schema": terminal["schema"],
        "terminal_sha256": terminal["terminal_sha256"],
        "generation": terminal["generation"],
        "round": terminal["round"],
        "member_id": terminal["member_id"],
        "task_id": terminal["task_id"],
        "quality_retry_index": terminal["quality_retry_index"],
        "output_redacted_sha256": terminal["output_redacted_sha256"],
        "session_id_sha256": terminal["session_id_sha256"],
        "session_mode": terminal.get("session_mode"),
        "base_prompt_sha256": terminal.get("base_prompt_sha256"),
        "effective_prompt_sha256": terminal.get("effective_prompt_sha256"),
        "lifecycle_context_receipt_sha256": terminal.get(
            "lifecycle_context_receipt_sha256"
        ),
        "workspace_mode": terminal["workspace_mode"],
        "workspace_base_sha256": terminal["workspace_base_sha256"],
        "workspace_result_sha256": terminal["workspace_result_sha256"],
        "telemetry_event_count": len(telemetry["events"]),
        "input_tokens": telemetry["input_tokens"],
        "output_tokens": telemetry["output_tokens"],
        "total_tokens": telemetry["total_tokens"],
        "cost_usd": telemetry["cost_usd"],
        "status": "provider-terminal-recovery-pending",
        "provider_replay_required": False,
    }


def _validate_terminal_telemetry(terminal: Dict) -> None:
    events = terminal["telemetry"]["events"]
    if any(event.get("provider") != "codex" for event in events):
        raise ValidationError("agent team turn terminal provider changed")
    sessions = [
        event.get("session_id")
        for event in events
        if str(event.get("event", "")).lower() == "thread.started"
    ]
    if not sessions or set(sessions) != {terminal["session_id"]}:
        raise ValidationError("agent team turn terminal session evidence changed")
    terminal_events = [
        event
        for event in events
        if str(event.get("event", "")).lower() in {"turn.completed", "turn_completed"}
    ]
    if len(terminal_events) != 1 or events[-1] is not terminal_events[0]:
        raise ValidationError("agent team turn terminal event evidence is invalid")


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
        raise ValidationError("agent team turn terminal %s is invalid" % label)


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)
