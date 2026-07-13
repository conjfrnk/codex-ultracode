import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .agent_packets import MAX_AGENT_ITEMS_PER_PACKET, MAX_AGENT_PACKETS
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


AGENT_MAP_PACKET_TERMINAL_SCHEMA = "conductor.agent_map_packet_terminal.v1"
MAX_AGENT_MAP_PACKET_TERMINAL_BYTES = 256 * 1024
MAX_AGENT_MAP_PACKET_TERMINAL_EVENTS = 64
MAX_AGENT_MAP_GENERATION = 10**9
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_FIELDS = {
    "schema",
    "step_id",
    "workflow_fingerprint",
    "index",
    "cache_generation",
    "packet_generation",
    "launch_pending_count",
    "source_item_count",
    "created_at_utc",
    "started_at_utc",
    "terminal_at_utc",
    "max_tokens",
    "cache_key",
    "item_sha256",
    "item_source_sha256",
    "prompt_sha256",
    "output",
    "output_raw_sha256",
    "output_redacted_sha256",
    "session_id",
    "session_id_sha256",
    "telemetry",
    "terminal_sha256",
}


def build_agent_map_packet_terminal(
    *,
    step_id: str,
    workflow_fingerprint: str,
    index: int,
    cache_generation: int,
    packet_generation: int,
    launch_pending_count: int,
    source_item_count: int,
    started_at_utc: str,
    terminal_at_utc: str,
    max_tokens: Optional[int],
    cache_key: str,
    item_sha256: str,
    item_source_sha256: str,
    prompt_sha256: str,
    output: str,
    output_raw_sha256: str,
    output_redacted_sha256: str,
    session_id: str,
    telemetry: ProviderTelemetry,
) -> Dict:
    terminal = {
        "schema": AGENT_MAP_PACKET_TERMINAL_SCHEMA,
        "step_id": step_id,
        "workflow_fingerprint": workflow_fingerprint,
        "index": index,
        "cache_generation": cache_generation,
        "packet_generation": packet_generation,
        "launch_pending_count": launch_pending_count,
        "source_item_count": source_item_count,
        "created_at_utc": terminal_at_utc,
        "started_at_utc": started_at_utc,
        "terminal_at_utc": terminal_at_utc,
        "max_tokens": max_tokens,
        "cache_key": cache_key,
        "item_sha256": item_sha256,
        "item_source_sha256": item_source_sha256,
        "prompt_sha256": prompt_sha256,
        "output": output,
        "output_raw_sha256": output_raw_sha256,
        "output_redacted_sha256": output_redacted_sha256,
        "session_id": session_id,
        "session_id_sha256": _sha256_text(session_id),
        "telemetry": agent_team_turn_telemetry_payload(telemetry),
    }
    terminal["terminal_sha256"] = _terminal_sha256(terminal)
    validate_agent_map_packet_terminal(terminal)
    return terminal


def validate_agent_map_packet_terminal(
    terminal: Dict,
    *,
    step: Optional[Dict] = None,
    expected: Optional[Dict] = None,
) -> None:
    if not isinstance(terminal, dict) or set(terminal) != TERMINAL_FIELDS:
        raise ValidationError("agent_map packet terminal has invalid fields")
    if terminal.get("schema") != AGENT_MAP_PACKET_TERMINAL_SCHEMA:
        raise ValidationError("agent_map packet terminal schema is invalid")
    _validate_id(terminal.get("step_id"), "agent_map packet terminal step_id")
    for field in (
        "workflow_fingerprint",
        "cache_key",
        "item_sha256",
        "item_source_sha256",
        "prompt_sha256",
        "output_raw_sha256",
        "output_redacted_sha256",
        "session_id_sha256",
        "terminal_sha256",
    ):
        _validate_sha256(terminal.get(field), "agent_map packet terminal %s" % field)
    _validate_int(terminal.get("index"), 1, MAX_AGENT_PACKETS, "index")
    _validate_int(
        terminal.get("cache_generation"),
        0,
        MAX_AGENT_MAP_GENERATION,
        "cache_generation",
    )
    _validate_int(
        terminal.get("packet_generation"),
        0,
        MAX_AGENT_MAP_GENERATION,
        "packet_generation",
    )
    _validate_int(
        terminal.get("launch_pending_count"),
        1,
        MAX_AGENT_PACKETS,
        "launch_pending_count",
    )
    _validate_int(
        terminal.get("source_item_count"),
        1,
        MAX_AGENT_ITEMS_PER_PACKET,
        "source_item_count",
    )
    for field in ("created_at_utc", "started_at_utc", "terminal_at_utc"):
        _validate_timestamp(terminal.get(field), "agent_map packet terminal %s" % field)
    if terminal["created_at_utc"] != terminal["terminal_at_utc"]:
        raise ValidationError("agent_map packet terminal creation timestamp changed")
    if _timestamp_value(terminal["started_at_utc"]) > _timestamp_value(
        terminal["terminal_at_utc"]
    ):
        raise ValidationError("agent_map packet terminal timestamps are inconsistent")
    max_tokens = terminal.get("max_tokens")
    if max_tokens is not None:
        _validate_int(
            max_tokens,
            MIN_CODEX_RUNTIME_TOKEN_CAP,
            MAX_PROVIDER_TOKENS,
            "max_tokens",
        )
    output = terminal.get("output")
    if not isinstance(output, str) or not output:
        raise ValidationError("agent_map packet terminal output is invalid")
    require_no_path_escape(output)
    _validate_uuid(terminal.get("session_id"), "agent_map packet terminal session_id")
    if _sha256_text(terminal["session_id"]) != terminal["session_id_sha256"]:
        raise ValidationError("agent_map packet terminal session hash changed")
    validate_agent_team_turn_telemetry(terminal.get("telemetry"))
    if len(terminal["telemetry"]["events"]) > MAX_AGENT_MAP_PACKET_TERMINAL_EVENTS:
        raise ValidationError("agent_map packet terminal has too many provider events")
    _validate_terminal_telemetry(terminal)
    if terminal["terminal_sha256"] != _terminal_sha256(terminal):
        raise ValidationError("agent_map packet terminal hash is invalid")
    if step is not None:
        if step.get("kind") != "agent_map" or terminal["step_id"] != step.get("id"):
            raise ValidationError("agent_map packet terminal step binding changed")
        if step.get("sandbox", "read-only") != "read-only":
            raise ValidationError("agent_map packet terminal sandbox authority changed")
    if expected is not None:
        if not isinstance(expected, dict):
            raise ValidationError("agent_map packet terminal expected binding is invalid")
        for field, value in expected.items():
            if field not in TERMINAL_FIELDS or field in {"schema", "terminal_sha256"}:
                raise ValidationError("agent_map packet terminal expected field is invalid")
            if terminal[field] != value:
                raise ValidationError("agent_map packet terminal %s binding changed" % field)


def agent_map_packet_terminal_path(run, capture_dir: str, index: int) -> Path:
    _validate_int(index, 1, MAX_AGENT_PACKETS, "index")
    relative = "%s/.agent-map-turn-terminals/%06d.json" % (capture_dir, index)
    require_no_path_escape(relative)
    path = run.resolve_artifact_path(relative)
    reject_symlink_path(path, "agent_map packet terminal")
    return path


def list_agent_map_packet_terminal_paths(
    run,
    capture_dir: str,
    packet_count: int,
) -> List[Path]:
    _validate_int(packet_count, 1, MAX_AGENT_PACKETS, "packet_count")
    relative = "%s/.agent-map-turn-terminals" % capture_dir
    require_no_path_escape(relative)
    directory = run.resolve_artifact_path(relative)
    reject_symlink_path(directory, "agent_map packet terminal directory")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValidationError("agent_map packet terminal directory is invalid")
    expected = {
        "%06d.json" % index: agent_map_packet_terminal_path(run, capture_dir, index)
        for index in range(1, packet_count + 1)
    }
    paths = []
    for path in sorted(directory.iterdir(), key=lambda value: value.name):
        reject_symlink_path(path, "agent_map packet terminal")
        if path.name not in expected or path != expected[path.name] or not path.is_file():
            raise ValidationError("agent_map packet terminal directory has an unknown entry")
        paths.append(path)
    return paths


def load_agent_map_packet_terminal(path: Path, **validation) -> Dict:
    reject_symlink_path(path, "agent_map packet terminal")
    text = read_regular_text_file_no_follow(
        path,
        "agent_map packet terminal",
        MAX_AGENT_MAP_PACKET_TERMINAL_BYTES,
    )
    try:
        terminal = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(
            "agent_map packet terminal is invalid JSON: %s" % exc.__class__.__name__
        )
    validate_agent_map_packet_terminal(terminal, **validation)
    return terminal


def write_agent_map_packet_terminal(run, capture_dir: str, terminal: Dict) -> Path:
    validate_agent_map_packet_terminal(terminal)
    path = agent_map_packet_terminal_path(run, capture_dir, terminal["index"])
    parent_fd = ensure_dir_no_follow(path.parent, "agent_map packet terminal parent")
    os.close(parent_fd)
    text = json.dumps(terminal, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_AGENT_MAP_PACKET_TERMINAL_BYTES:
        raise ValidationError("agent_map packet terminal exceeds its byte limit")
    write_new_text_file_no_follow(path, "agent_map packet terminal", text)
    return path


def remove_agent_map_packet_terminal(path: Path) -> None:
    unlink_regular_file_no_follow(path, "agent_map packet terminal")


def provider_telemetry_from_agent_map_packet_terminal(terminal: Dict) -> ProviderTelemetry:
    validate_agent_map_packet_terminal(terminal)
    return provider_telemetry_from_agent_team_turn_telemetry(terminal["telemetry"])


def agent_map_packet_terminal_summary(terminal: Dict) -> Dict:
    validate_agent_map_packet_terminal(terminal)
    telemetry = terminal["telemetry"]
    return {
        "schema": terminal["schema"],
        "terminal_sha256": terminal["terminal_sha256"],
        "step_id": terminal["step_id"],
        "index": terminal["index"],
        "cache_generation": terminal["cache_generation"],
        "packet_generation": terminal["packet_generation"],
        "launch_pending_count": terminal["launch_pending_count"],
        "source_item_count": terminal["source_item_count"],
        "cache_key": terminal["cache_key"],
        "item_source_sha256": terminal["item_source_sha256"],
        "prompt_sha256": terminal["prompt_sha256"],
        "output_redacted_sha256": terminal["output_redacted_sha256"],
        "session_id_sha256": terminal["session_id_sha256"],
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
        raise ValidationError("agent_map packet terminal provider changed")
    sessions = [
        event.get("session_id")
        for event in events
        if str(event.get("event", "")).lower() == "thread.started"
    ]
    if not sessions or set(sessions) != {terminal["session_id"]}:
        raise ValidationError("agent_map packet terminal session evidence changed")
    terminal_events = [
        event
        for event in events
        if str(event.get("event", "")).lower() in {"turn.completed", "turn_completed"}
    ]
    if len(terminal_events) != 1 or events[-1] is not terminal_events[0]:
        raise ValidationError("agent_map packet terminal event evidence is invalid")


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
        raise ValidationError("agent_map packet terminal %s is invalid" % label)


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)
