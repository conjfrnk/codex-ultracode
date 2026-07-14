import hashlib
import json
import math
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
from .codex_config import MIN_CODEX_RUNTIME_TOKEN_CAP
from .errors import ValidationError
from .provider_telemetry import MAX_PROVIDER_EVENTS, MAX_PROVIDER_TOKENS, ProviderTelemetry
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    require_no_path_escape,
    unlink_regular_file_no_follow,
    write_new_text_file_no_follow,
)
from .staged_workspace import workspace_snapshot_from_manifest


AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1 = "conductor.agent_team_turn_completion.v1"
AGENT_TEAM_TURN_COMPLETION_SCHEMA = "conductor.agent_team_turn_completion.v2"
MAX_AGENT_TEAM_TURN_COMPLETION_BYTES = MAX_AGENT_TEAM_MERGE_TRANSACTION_BYTES
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMPLETION_FIELDS_V1 = {
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
    "finished_at_utc",
    "max_tokens",
    "output",
    "output_sha256",
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
    "completion_sha256",
}
COMPLETION_FIELDS = COMPLETION_FIELDS_V1 | {
    "session_mode",
    "base_prompt_sha256",
    "effective_prompt_sha256",
    "lifecycle_context_receipt_sha256",
}
TELEMETRY_FIELDS = {
    "events",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
}
TELEMETRY_EVENT_REQUIRED_FIELDS = {"event", "provider"}
TELEMETRY_EVENT_OPTIONAL_TEXT_FIELDS = {"status", "session_id", "model"}
TELEMETRY_EVENT_OPTIONAL_NUMBER_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
}
TELEMETRY_EVENT_FIELDS = (
    TELEMETRY_EVENT_REQUIRED_FIELDS
    | TELEMETRY_EVENT_OPTIONAL_TEXT_FIELDS
    | TELEMETRY_EVENT_OPTIONAL_NUMBER_FIELDS
)


def build_agent_team_turn_completion(
    *,
    step: Dict,
    state: Dict,
    workflow_fingerprint: str,
    member_id: str,
    task_id: str,
    quality_retry_index: int,
    output: str,
    output_sha256: str,
    session_id: str,
    started_at_utc: str,
    finished_at_utc: str,
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
    if not isinstance(telemetry, ProviderTelemetry):
        raise ValidationError("agent team turn completion telemetry is invalid")
    write_team = step.get("sandbox", "read-only") == "workspace-write"
    workspace_mode = "isolated-write" if write_team else "read-only"
    workspace_base_sha256 = None
    if write_team:
        workspace_base = workspace_snapshot_from_manifest(workspace_base_manifest)
        workspace_base_sha256 = workspace_base.tracked_fingerprint_sha256
    completion = {
        "schema": AGENT_TEAM_TURN_COMPLETION_SCHEMA,
        "step_id": step["id"],
        "workflow_fingerprint": workflow_fingerprint,
        "generation": state["generation"],
        "round": state["round"],
        "member_id": member_id,
        "task_id": task_id,
        "quality_retry_index": quality_retry_index,
        "created_at_utc": finished_at_utc,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "max_tokens": max_tokens,
        "output": output,
        "output_sha256": output_sha256,
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
    completion["completion_sha256"] = _completion_sha256(completion)
    validate_agent_team_turn_completion(
        completion,
        step=step,
        state=state,
        workflow_fingerprint=workflow_fingerprint,
    )
    return completion


def build_agent_team_turn_completion_from_terminal(
    *,
    terminal: Dict,
    output_sha256: str,
    workspace_result_sha256: Optional[str] = None,
) -> Dict:
    from .agent_team_turn_terminal import (
        AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1,
        validate_agent_team_turn_terminal,
    )

    validate_agent_team_turn_terminal(terminal)
    if output_sha256 != terminal["output_redacted_sha256"]:
        raise ValidationError("agent team terminal redacted output hash changed")
    if (
        terminal["workspace_mode"] == "isolated-write"
        and workspace_result_sha256 != terminal["workspace_result_sha256"]
    ):
        raise ValidationError("agent team terminal workspace result changed")
    current_terminal = terminal["schema"] != AGENT_TEAM_TURN_TERMINAL_SCHEMA_V1
    completion = {
        "schema": (
            AGENT_TEAM_TURN_COMPLETION_SCHEMA
            if current_terminal
            else AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1
        ),
        "step_id": terminal["step_id"],
        "workflow_fingerprint": terminal["workflow_fingerprint"],
        "generation": terminal["generation"],
        "round": terminal["round"],
        "member_id": terminal["member_id"],
        "task_id": terminal["task_id"],
        "quality_retry_index": terminal["quality_retry_index"],
        "created_at_utc": terminal["terminal_at_utc"],
        "started_at_utc": terminal["started_at_utc"],
        "finished_at_utc": terminal["terminal_at_utc"],
        "max_tokens": terminal["max_tokens"],
        "output": terminal["output"],
        "output_sha256": output_sha256,
        "session_id": terminal["session_id"],
        "session_id_sha256": terminal["session_id_sha256"],
        "launch_state_sha256": terminal["launch_state_sha256"],
        "launch_turn_count": terminal["launch_turn_count"],
        "launch_task_count": terminal["launch_task_count"],
        "launch_message_count": terminal["launch_message_count"],
        "launch_authorized_tokens": terminal["launch_authorized_tokens"],
        "telemetry": json.loads(json.dumps(terminal["telemetry"])),
        "workspace_mode": terminal["workspace_mode"],
        "workspace_relative": terminal["workspace_relative"],
        "workspace_base_manifest": json.loads(
            json.dumps(terminal["workspace_base_manifest"])
        ),
        "workspace_base_sha256": terminal["workspace_base_sha256"],
        "workspace_result_sha256": (
            workspace_result_sha256
            if terminal["workspace_mode"] == "isolated-write"
            else None
        ),
    }
    if current_terminal:
        completion.update(
            {
                "effective_prompt_sha256": terminal["effective_prompt_sha256"],
                "base_prompt_sha256": terminal["base_prompt_sha256"],
                "session_mode": terminal["session_mode"],
                "lifecycle_context_receipt_sha256": terminal[
                    "lifecycle_context_receipt_sha256"
                ],
            }
        )
    completion["completion_sha256"] = _completion_sha256(completion)
    validate_agent_team_turn_completion(completion)
    return completion


def validate_agent_team_turn_completion(
    completion: Dict,
    *,
    step: Optional[Dict] = None,
    state: Optional[Dict] = None,
    workflow_fingerprint: Optional[str] = None,
) -> None:
    if not isinstance(completion, dict):
        raise ValidationError("agent team turn completion has invalid fields")
    schema = completion.get("schema")
    expected_fields = (
        COMPLETION_FIELDS
        if schema == AGENT_TEAM_TURN_COMPLETION_SCHEMA
        else COMPLETION_FIELDS_V1
        if schema == AGENT_TEAM_TURN_COMPLETION_SCHEMA_V1
        else None
    )
    if expected_fields is None:
        raise ValidationError("agent team turn completion schema is invalid")
    if set(completion) != expected_fields:
        raise ValidationError("agent team turn completion has invalid fields")
    for field in ("step_id", "member_id", "task_id"):
        _validate_id(completion.get(field), "agent team turn completion %s" % field)
    _validate_sha256(
        completion.get("workflow_fingerprint"),
        "agent team turn completion workflow fingerprint",
    )
    if (
        workflow_fingerprint is not None
        and completion["workflow_fingerprint"] != workflow_fingerprint
    ):
        raise ValidationError("agent team turn completion workflow fingerprint changed")
    _validate_int(
        completion.get("generation"),
        0,
        MAX_AGENT_TEAM_GENERATION,
        "generation",
    )
    _validate_int(completion.get("round"), 1, MAX_AGENT_TEAM_ROUNDS, "round")
    _validate_int(
        completion.get("quality_retry_index"),
        0,
        MAX_AGENT_TEAM_QUALITY_RETRIES,
        "quality_retry_index",
    )
    _validate_int(
        completion.get("max_tokens"),
        MIN_CODEX_RUNTIME_TOKEN_CAP,
        MAX_PROVIDER_TOKENS,
        "max_tokens",
    )
    for field in ("created_at_utc", "started_at_utc", "finished_at_utc"):
        _validate_timestamp(completion.get(field), "agent team turn completion %s" % field)
    if completion["created_at_utc"] != completion["finished_at_utc"]:
        raise ValidationError("agent team turn completion creation timestamp changed")
    if _timestamp_value(completion["started_at_utc"]) > _timestamp_value(
        completion["finished_at_utc"]
    ):
        raise ValidationError("agent team turn completion timestamps are inconsistent")
    output = completion.get("output")
    if not isinstance(output, str) or not output:
        raise ValidationError("agent team turn completion output is invalid")
    require_no_path_escape(output)
    for field in (
        "output_sha256",
        "session_id_sha256",
        "launch_state_sha256",
        "completion_sha256",
    ):
        _validate_sha256(
            completion.get(field),
            "agent team turn completion %s" % field,
        )
    if schema == AGENT_TEAM_TURN_COMPLETION_SCHEMA:
        if completion.get("session_mode") not in {"new", "resume"}:
            raise ValidationError("agent team turn completion session mode is invalid")
        _validate_sha256(
            completion.get("base_prompt_sha256"),
            "agent team turn completion base prompt",
        )
        _validate_sha256(
            completion.get("effective_prompt_sha256"),
            "agent team turn completion effective prompt",
        )
        receipt_sha256 = completion.get("lifecycle_context_receipt_sha256")
        if receipt_sha256 is not None:
            _validate_sha256(
                receipt_sha256,
                "agent team turn completion lifecycle context receipt",
            )
    _validate_int(
        completion.get("launch_turn_count"),
        0,
        MAX_AGENT_TEAM_TURNS,
        "launch_turn_count",
    )
    _validate_int(
        completion.get("launch_task_count"),
        1,
        MAX_AGENT_TEAM_TASKS,
        "launch_task_count",
    )
    _validate_int(
        completion.get("launch_message_count"),
        0,
        MAX_AGENT_TEAM_MESSAGES,
        "launch_message_count",
    )
    _validate_int(
        completion.get("launch_authorized_tokens"),
        MIN_CODEX_RUNTIME_TOKEN_CAP,
        MAX_PROVIDER_TOKENS,
        "launch_authorized_tokens",
    )
    _validate_uuid(completion.get("session_id"), "agent team turn completion session_id")
    if _sha256_text(completion["session_id"]) != completion["session_id_sha256"]:
        raise ValidationError("agent team turn completion session hash changed")
    _validate_telemetry(completion.get("telemetry"))
    workspace_mode = completion.get("workspace_mode")
    if workspace_mode not in {"read-only", "isolated-write"}:
        raise ValidationError("agent team turn completion workspace mode is invalid")
    if workspace_mode == "read-only":
        if any(
            completion[field] is not None
            for field in (
                "workspace_relative",
                "workspace_base_manifest",
                "workspace_base_sha256",
                "workspace_result_sha256",
            )
        ):
            raise ValidationError("read-only agent team turn completion has workspace evidence")
    else:
        workspace_relative = completion.get("workspace_relative")
        if not isinstance(workspace_relative, str) or not workspace_relative:
            raise ValidationError("agent team turn completion workspace path is invalid")
        require_no_path_escape(workspace_relative)
        workspace_base = workspace_snapshot_from_manifest(
            completion.get("workspace_base_manifest")
        )
        _validate_sha256(
            completion.get("workspace_base_sha256"),
            "agent team turn completion workspace base hash",
        )
        _validate_sha256(
            completion.get("workspace_result_sha256"),
            "agent team turn completion workspace result hash",
        )
        if (
            workspace_base.tracked_fingerprint_sha256
            != completion["workspace_base_sha256"]
        ):
            raise ValidationError("agent team turn completion workspace base changed")
    if completion["completion_sha256"] != _completion_sha256(completion):
        raise ValidationError("agent team turn completion hash is invalid")
    if step is not None:
        if completion["step_id"] != step.get("id"):
            raise ValidationError("agent team turn completion step binding changed")
        expected_mode = (
            "isolated-write"
            if step.get("sandbox", "read-only") == "workspace-write"
            else "read-only"
        )
        if completion["workspace_mode"] != expected_mode:
            raise ValidationError("agent team turn completion workspace authority changed")
        expected_output = "%s/round-%03d/%s--%s.json" % (
            step["capture_dir"],
            completion["round"],
            completion["member_id"],
            completion["task_id"],
        )
        if completion["output"] != expected_output:
            raise ValidationError("agent team turn completion output binding changed")
    if state is not None:
        validate_agent_team_state(
            state,
            step=step,
            workflow_fingerprint=completion["workflow_fingerprint"],
            generation=completion["generation"],
        )
        if agent_team_state_sha256(state) != completion["launch_state_sha256"]:
            raise ValidationError("agent team turn completion launch state changed")
        if state["round"] != completion["round"]:
            raise ValidationError("agent team turn completion launch round changed")
        member = next(
            (value for value in state["members"] if value["id"] == completion["member_id"]),
            None,
        )
        task = next(
            (value for value in state["tasks"] if value["id"] == completion["task_id"]),
            None,
        )
        if (
            member is None
            or task is None
            or member["current_task_id"] != completion["task_id"]
            or task["claimed_by"] != completion["member_id"]
        ):
            raise ValidationError("agent team turn completion launch claim changed")
        if (
            len(state["turns"]) != completion["launch_turn_count"]
            or len(state["tasks"]) != completion["launch_task_count"]
            or len(state["messages"]) != completion["launch_message_count"]
            or state["authorized_tokens"] != completion["launch_authorized_tokens"]
        ):
            raise ValidationError("agent team turn completion launch counts changed")


def agent_team_turn_completion_path(run, step: Dict, member_id: str) -> Path:
    _validate_id(member_id, "agent team turn completion member_id")
    relative = "%s/turn-completions/%s.json" % (step["capture_dir"], member_id)
    require_no_path_escape(relative)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent team turn completion")
    return path


def list_agent_team_turn_completion_paths(run, step: Dict) -> List[Path]:
    relative = "%s/turn-completions" % step["capture_dir"]
    require_no_path_escape(relative)
    directory = run.resolve_artifact_path(relative)
    reject_symlink_path(directory, "agent team turn completion directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent team turn completion directory is invalid")
    expected = {
        "%s.json" % member["id"]: agent_team_turn_completion_path(
            run,
            step,
            member["id"],
        )
        for member in step["members"]
    }
    paths = []
    for path in sorted(directory.iterdir(), key=lambda value: value.name):
        reject_symlink_path(path, "agent team turn completion")
        if path.name not in expected or path != expected[path.name] or not path.is_file():
            raise ValidationError("agent team turn completion directory has an unknown entry")
        paths.append(path)
    return paths


def load_agent_team_turn_completion(path: Path, **validation) -> Dict:
    reject_symlink_path(path, "agent team turn completion")
    text = read_regular_text_file_no_follow(
        path,
        "agent team turn completion",
        MAX_AGENT_TEAM_TURN_COMPLETION_BYTES,
    )
    try:
        completion = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent team turn completion is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_team_turn_completion(completion, **validation)
    return completion


def write_agent_team_turn_completion(run, step: Dict, completion: Dict) -> Path:
    validate_agent_team_turn_completion(completion, step=step)
    path = agent_team_turn_completion_path(run, step, completion["member_id"])
    parent_fd = ensure_dir_no_follow(path.parent, "agent team turn completion parent")
    os.close(parent_fd)
    text = json.dumps(completion, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_TEAM_TURN_COMPLETION_BYTES:
        raise ValidationError("agent team turn completion exceeds its byte limit")
    write_new_text_file_no_follow(path, "agent team turn completion", text)
    return path


def remove_agent_team_turn_completion(path: Path) -> None:
    unlink_regular_file_no_follow(path, "agent team turn completion")


def provider_telemetry_from_turn_completion(completion: Dict) -> ProviderTelemetry:
    validate_agent_team_turn_completion(completion)
    return provider_telemetry_from_agent_team_turn_telemetry(completion["telemetry"])


def agent_team_turn_telemetry_payload(telemetry: ProviderTelemetry) -> Dict:
    if not isinstance(telemetry, ProviderTelemetry):
        raise ValidationError("agent team turn telemetry is invalid")
    value = {
        "events": json.loads(json.dumps(telemetry.events)),
        "input_tokens": telemetry.input_tokens,
        "output_tokens": telemetry.output_tokens,
        "total_tokens": telemetry.total_tokens,
        "cost_usd": telemetry.cost_usd,
    }
    validate_agent_team_turn_telemetry(value)
    return value


def provider_telemetry_from_agent_team_turn_telemetry(telemetry: Dict) -> ProviderTelemetry:
    validate_agent_team_turn_telemetry(telemetry)
    return ProviderTelemetry(
        events=json.loads(json.dumps(telemetry["events"])),
        input_tokens=telemetry["input_tokens"],
        output_tokens=telemetry["output_tokens"],
        total_tokens=telemetry["total_tokens"],
        cost_usd=telemetry["cost_usd"],
    )


def agent_team_turn_completion_summary(completion: Dict) -> Dict:
    validate_agent_team_turn_completion(completion)
    telemetry = completion["telemetry"]
    return {
        "schema": completion["schema"],
        "completion_sha256": completion["completion_sha256"],
        "generation": completion["generation"],
        "round": completion["round"],
        "member_id": completion["member_id"],
        "task_id": completion["task_id"],
        "quality_retry_index": completion["quality_retry_index"],
        "output_sha256": completion["output_sha256"],
        "session_id_sha256": completion["session_id_sha256"],
        "session_mode": completion.get("session_mode"),
        "base_prompt_sha256": completion.get("base_prompt_sha256"),
        "effective_prompt_sha256": completion.get("effective_prompt_sha256"),
        "lifecycle_context_receipt_sha256": completion.get(
            "lifecycle_context_receipt_sha256"
        ),
        "workspace_mode": completion["workspace_mode"],
        "workspace_base_sha256": completion["workspace_base_sha256"],
        "workspace_result_sha256": completion["workspace_result_sha256"],
        "telemetry_event_count": len(telemetry["events"]),
        "input_tokens": telemetry["input_tokens"],
        "output_tokens": telemetry["output_tokens"],
        "total_tokens": telemetry["total_tokens"],
        "cost_usd": telemetry["cost_usd"],
        "status": "provider-completed-recovery-pending",
        "provider_replay_required": False,
    }


def validate_agent_team_turn_telemetry(telemetry) -> None:
    if not isinstance(telemetry, dict) or set(telemetry) != TELEMETRY_FIELDS:
        raise ValidationError("agent team turn completion telemetry has invalid fields")
    events = telemetry.get("events")
    if not isinstance(events, list) or len(events) > MAX_PROVIDER_EVENTS:
        raise ValidationError("agent team turn completion telemetry events are invalid")
    for event in events:
        if (
            not isinstance(event, dict)
            or not TELEMETRY_EVENT_REQUIRED_FIELDS <= set(event)
            or not set(event) <= TELEMETRY_EVENT_FIELDS
        ):
            raise ValidationError("agent team turn completion telemetry event is invalid")
        for field in TELEMETRY_EVENT_REQUIRED_FIELDS | TELEMETRY_EVENT_OPTIONAL_TEXT_FIELDS:
            if field not in event:
                continue
            value = event[field]
            if not isinstance(value, str) or not value or len(value) > 300:
                raise ValidationError(
                    "agent team turn completion telemetry event %s is invalid" % field
                )
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            if field in event:
                _validate_int(event[field], 0, MAX_PROVIDER_TOKENS, field)
        if "input_tokens" in event and "output_tokens" in event:
            derived = event["input_tokens"] + event["output_tokens"]
            if derived > MAX_PROVIDER_TOKENS or event.get("total_tokens") != derived:
                raise ValidationError(
                    "agent team turn completion telemetry event totals are inconsistent"
                )
        if "cost_usd" in event:
            _validate_cost(event["cost_usd"], "event cost_usd")
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = telemetry.get(field)
        if value is not None:
            _validate_int(value, 0, MAX_PROVIDER_TOKENS, field)
    cost = telemetry.get("cost_usd")
    if cost is not None:
        _validate_cost(cost, "cost_usd")
    if telemetry["input_tokens"] is not None and telemetry["output_tokens"] is not None:
        derived = telemetry["input_tokens"] + telemetry["output_tokens"]
        if derived > MAX_PROVIDER_TOKENS or telemetry["total_tokens"] != derived:
            raise ValidationError("agent team turn completion telemetry totals are inconsistent")


_validate_telemetry = validate_agent_team_turn_telemetry


def _completion_sha256(completion: Dict) -> str:
    value = dict(completion)
    value.pop("completion_sha256", None)
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
        raise ValidationError("agent team turn completion %s is invalid" % label)


def _validate_cost(value, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or not math.isfinite(value)
    ):
        raise ValidationError("agent team turn completion %s is invalid" % label)


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)
