import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .errors import ValidationError
from .codex_config import (
    CODEX_NATIVE_AGENT_MAX_DEPTH,
    MAX_CODEX_NATIVE_AGENT_THREADS,
    MIN_CODEX_NATIVE_AGENT_THREADS,
)
from .codex_native_usage import (
    NATIVE_USAGE_STATUSES,
    empty_native_usage,
    validate_native_usage,
)
from .provider_telemetry import MAX_PROVIDER_TOKENS, parse_provider_jsonl
from .security import (
    ensure_dir_no_follow,
    open_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
)


CODEX_PROGRESS_SCHEMA_V1 = "conductor.codex_progress.v1"
CODEX_PROGRESS_SCHEMA_V2 = "conductor.codex_progress.v2"
CODEX_PROGRESS_SCHEMA_V3 = "conductor.codex_progress.v3"
CODEX_PROGRESS_SCHEMA = "conductor.codex_progress.v4"
MAX_CODEX_PROGRESS_BYTES = 64 * 1024
MAX_CODEX_PROGRESS_EVENTS = 1_000_000
MAX_CODEX_PROGRESS_ATTEMPTS = 1_000_000_000
MAX_CODEX_PROGRESS_PACKET_INDEX = 10_000
MAX_CODEX_PROGRESS_NATIVE_AGENTS = 4096
CODEX_PROGRESS_WRITE_INTERVAL_SECONDS = 0.25
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROGRESS_STATUSES = {"active", "completed", "failed", "timed-out", "interrupted"}
LAST_EVENTS_V1 = {
    "launch",
    "thread-started",
    "turn-started",
    "item-started",
    "item-completed",
    "turn-completed",
    "turn-failed",
    "error",
    "other",
}
LAST_EVENTS = LAST_EVENTS_V1 | {"item-updated"}
ITEM_COUNT_FIELDS_V1 = {
    "agent_message",
    "reasoning",
    "command_execution",
    "file_change",
    "tool_call",
    "web_search",
    "plan",
    "error",
    "other",
}
ITEM_COUNT_FIELDS = ITEM_COUNT_FIELDS_V1 | {"collab_tool_call"}
COLLAB_TOOL_COUNT_FIELDS = {
    "spawn_agent",
    "send_input",
    "wait",
    "close_agent",
    "other",
}
NATIVE_AGENT_STATUS_FIELDS = {
    "pending_init",
    "running",
    "interrupted",
    "completed",
    "errored",
    "shutdown",
    "not_found",
    "unknown",
}
NATIVE_AGENT_ACTIVE_STATUSES = {"pending_init", "running"}
NATIVE_AGENT_ERROR_STATUSES = {"errored", "not_found"}
PROGRESS_FIELDS_V1 = {
    "schema",
    "status",
    "scope",
    "step_id",
    "packet_index",
    "workflow_fingerprint",
    "invocation_id",
    "attempt",
    "started_at_utc",
    "updated_at_utc",
    "finished_at_utc",
    "sandbox",
    "model",
    "effort",
    "max_tokens",
    "session_id_sha256",
    "event_count",
    "events_truncated",
    "turn_started_count",
    "turn_completed_count",
    "item_started_count",
    "item_completed_count",
    "failed_item_count",
    "item_counts",
    "last_event",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "progress_sha256",
}
PROGRESS_FIELDS_V2 = PROGRESS_FIELDS_V1 | {
    "collab_tool_counts",
    "collab_tool_failed_count",
    "native_agent_count",
    "native_agent_status_counts",
    "native_agent_observation_truncated",
    "native_agent_usage_attributed",
    "native_agents_enabled_by_observer",
}
PROGRESS_FIELDS_V3 = PROGRESS_FIELDS_V2 | {
    "native_agents_enabled_by_runner",
    "native_agent_max_threads",
    "native_agent_max_depth",
}
PROGRESS_FIELDS = PROGRESS_FIELDS_V3 | {
    "native_agent_usage_status",
    "native_agent_usage_session_count",
    "native_agent_usage_child_count",
    "native_agent_usage_input_tokens",
    "native_agent_usage_cached_input_tokens",
    "native_agent_usage_output_tokens",
    "native_agent_usage_total_tokens",
    "native_agent_usage_rollout_tokens",
}


class CodexProgressObserver:
    def __init__(
        self,
        *,
        path: Path,
        scope: str,
        step_id: str,
        packet_index: Optional[int],
        workflow_fingerprint: str,
        attempt: int,
        started_at_utc: str,
        sandbox: str,
        model: Optional[str],
        effort: Optional[str],
        max_tokens: Optional[int],
        native_agent_max_threads: Optional[int] = None,
        write_interval_seconds: float = CODEX_PROGRESS_WRITE_INTERVAL_SECONDS,
    ):
        self.path = Path(path)
        self.write_interval_seconds = max(0.0, float(write_interval_seconds))
        self._last_write_monotonic = 0.0
        self._provider_terminal = False
        self._disabled = False
        self._native_agent_states: Dict[str, str] = {}
        if native_agent_max_threads is not None and (
            isinstance(native_agent_max_threads, bool)
            or not isinstance(native_agent_max_threads, int)
            or native_agent_max_threads < MIN_CODEX_NATIVE_AGENT_THREADS
            or native_agent_max_threads > MAX_CODEX_NATIVE_AGENT_THREADS
        ):
            raise ValidationError("Codex progress native agent max_threads is invalid")
        self.progress = {
            "schema": CODEX_PROGRESS_SCHEMA,
            "status": "active",
            "scope": scope,
            "step_id": step_id,
            "packet_index": packet_index,
            "workflow_fingerprint": workflow_fingerprint,
            "invocation_id": str(uuid.uuid4()),
            "attempt": attempt,
            "started_at_utc": started_at_utc,
            "updated_at_utc": started_at_utc,
            "finished_at_utc": None,
            "sandbox": sandbox,
            "model": model,
            "effort": effort,
            "max_tokens": max_tokens,
            "session_id_sha256": None,
            "event_count": 0,
            "events_truncated": False,
            "turn_started_count": 0,
            "turn_completed_count": 0,
            "item_started_count": 0,
            "item_completed_count": 0,
            "failed_item_count": 0,
            "item_counts": {name: 0 for name in sorted(ITEM_COUNT_FIELDS)},
            "collab_tool_counts": {
                name: 0 for name in sorted(COLLAB_TOOL_COUNT_FIELDS)
            },
            "collab_tool_failed_count": 0,
            "native_agent_count": 0,
            "native_agent_status_counts": {
                name: 0 for name in sorted(NATIVE_AGENT_STATUS_FIELDS)
            },
            "native_agent_observation_truncated": False,
            "native_agent_usage_attributed": False,
            "native_agents_enabled_by_observer": False,
            "native_agents_enabled_by_runner": native_agent_max_threads is not None,
            "native_agent_max_threads": native_agent_max_threads,
            "native_agent_max_depth": (
                CODEX_NATIVE_AGENT_MAX_DEPTH
                if native_agent_max_threads is not None
                else None
            ),
            "native_agent_usage_status": (
                "pending" if native_agent_max_threads is not None else "not-requested"
            ),
            "native_agent_usage_session_count": 0,
            "native_agent_usage_child_count": 0,
            "native_agent_usage_input_tokens": None,
            "native_agent_usage_cached_input_tokens": None,
            "native_agent_usage_output_tokens": None,
            "native_agent_usage_total_tokens": None,
            "native_agent_usage_rollout_tokens": None,
            "last_event": "launch",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "progress_sha256": "0" * 64,
        }
        self._write(force=True)

    @property
    def provider_terminal(self) -> bool:
        return self._provider_terminal

    @property
    def disabled(self) -> bool:
        return self._disabled

    def observe(self, line: str) -> None:
        if self._disabled or self._provider_terminal:
            return
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        event_name = str(raw.get("type") or raw.get("event") or "").strip().lower()
        if not event_name:
            return
        self._increment("event_count")
        force = False
        if event_name == "thread.started":
            session_id = raw.get("thread_id") or raw.get("session_id")
            if _canonical_uuid(session_id):
                self.progress["session_id_sha256"] = _sha256_text(session_id)
            self.progress["last_event"] = "thread-started"
            force = True
        elif event_name in {"turn.started", "turn_started"}:
            self._increment("turn_started_count")
            self.progress["last_event"] = "turn-started"
            force = True
        elif event_name in {"item.started", "item_started"}:
            self._increment("item_started_count")
            item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
            if _item_kind(item.get("type")) == "collab_tool_call":
                self._observe_collab_item(item, count_completed=False)
                force = True
            self.progress["last_event"] = "item-started"
        elif event_name in {"item.updated", "item_updated"}:
            item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
            if _item_kind(item.get("type")) == "collab_tool_call":
                self._observe_collab_item(item, count_completed=False)
                force = True
            self.progress["last_event"] = "item-updated"
        elif event_name in {"item.completed", "item_completed"}:
            counted = self._increment("item_completed_count")
            item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
            item_kind = _item_kind(item.get("type"))
            if counted:
                self.progress["item_counts"][item_kind] += 1
            status = str(item.get("status") or "").strip().lower()
            if counted and (item_kind == "error" or status in {"failed", "declined", "error"}):
                self._increment("failed_item_count")
            if item_kind == "collab_tool_call":
                self._observe_collab_item(item, count_completed=counted)
                force = True
            self.progress["last_event"] = "item-completed"
        elif event_name in {"turn.completed", "turn_completed"}:
            self._increment("turn_completed_count")
            self.progress["last_event"] = "turn-completed"
            self._provider_terminal = True
            self._set_usage(line)
            timestamp = _utc_now()
            self._set_status("completed", timestamp)
            self.progress["updated_at_utc"] = timestamp
            force = True
        elif event_name in {"turn.failed", "turn_failed"}:
            self.progress["last_event"] = "turn-failed"
            self._provider_terminal = True
            timestamp = _utc_now()
            self._set_status("failed", timestamp)
            self.progress["updated_at_utc"] = timestamp
            force = True
        elif event_name == "error":
            self.progress["last_event"] = "error"
            force = True
        else:
            self.progress["last_event"] = "other"
        if not self._provider_terminal:
            self.progress["updated_at_utc"] = _utc_now()
        self._write(force=force)

    def finalize(self, status: str, *, preserve_provider_terminal: bool = False) -> Dict:
        if status not in PROGRESS_STATUSES - {"active"}:
            raise ValidationError("Codex progress terminal status is invalid")
        if preserve_provider_terminal and self._provider_terminal:
            status = self.progress["status"]
        timestamp = _utc_now()
        if self.progress["native_agent_usage_status"] == "pending":
            self._apply_native_usage(empty_native_usage("unavailable"))
        self._set_status(status, timestamp)
        self.progress["updated_at_utc"] = timestamp
        self._write(force=True)
        return dict(self.progress)

    def set_native_usage(self, usage: Dict) -> None:
        if self._disabled:
            return
        validate_native_usage(usage)
        if not self.progress["native_agents_enabled_by_runner"]:
            raise ValidationError("Codex progress cannot attribute unrequested native usage")
        if usage["status"] == "not-requested":
            raise ValidationError("enabled Codex progress cannot use not-requested native usage")
        self._apply_native_usage(usage)
        timestamp = _utc_now()
        self.progress["updated_at_utc"] = timestamp
        if self.progress["status"] != "active":
            self.progress["finished_at_utc"] = timestamp
        self._write(force=True)

    def _apply_native_usage(self, usage: Dict) -> None:
        self.progress["native_agent_usage_attributed"] = usage["status"] == "complete"
        self.progress["native_agent_usage_status"] = usage["status"]
        self.progress["native_agent_usage_session_count"] = usage["session_count"]
        self.progress["native_agent_usage_child_count"] = usage["child_count"]
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
            "rollout_tokens",
        ):
            self.progress["native_agent_usage_%s" % field] = usage[field]

    def _set_status(self, status: str, timestamp: str) -> None:
        self.progress["status"] = status
        self.progress["finished_at_utc"] = timestamp if status != "active" else None

    def _set_usage(self, line: str) -> None:
        try:
            telemetry = parse_provider_jsonl(line, "codex")
        except ValidationError:
            return
        self.progress["input_tokens"] = telemetry.input_tokens
        self.progress["output_tokens"] = telemetry.output_tokens
        self.progress["total_tokens"] = telemetry.total_tokens

    def _increment(self, field: str) -> bool:
        current = self.progress[field]
        if current >= MAX_CODEX_PROGRESS_EVENTS:
            self.progress["events_truncated"] = True
            return False
        self.progress[field] = current + 1
        return True

    def _observe_collab_item(self, item: Dict, *, count_completed: bool) -> None:
        if count_completed:
            tool = _collab_tool(item.get("tool"))
            self.progress["collab_tool_counts"][tool] += 1
            status = str(item.get("status") or "").strip().lower()
            if status in {"failed", "declined", "error"}:
                self.progress["collab_tool_failed_count"] += 1

        receiver_ids = item.get("receiver_thread_ids")
        if isinstance(receiver_ids, list):
            for thread_id in receiver_ids:
                self._remember_native_agent(thread_id, None)

        agent_states = item.get("agents_states")
        if isinstance(agent_states, dict):
            for thread_id, state in agent_states.items():
                self._remember_native_agent(thread_id, _native_agent_status(state))
        self._sync_native_agent_counts()

    def _remember_native_agent(self, thread_id, status: Optional[str]) -> None:
        if not _canonical_uuid(thread_id):
            return
        if thread_id not in self._native_agent_states:
            if len(self._native_agent_states) >= MAX_CODEX_PROGRESS_NATIVE_AGENTS:
                self.progress["native_agent_observation_truncated"] = True
                return
            self._native_agent_states[thread_id] = "unknown"
        if status is not None:
            self._native_agent_states[thread_id] = status

    def _sync_native_agent_counts(self) -> None:
        counts = {name: 0 for name in sorted(NATIVE_AGENT_STATUS_FIELDS)}
        for status in self._native_agent_states.values():
            counts[status] += 1
        self.progress["native_agent_count"] = len(self._native_agent_states)
        self.progress["native_agent_status_counts"] = counts

    def _write(self, *, force: bool) -> None:
        if self._disabled:
            return
        now = time.monotonic()
        if not force and now - self._last_write_monotonic < self.write_interval_seconds:
            return
        try:
            value = dict(self.progress)
            value["item_counts"] = dict(self.progress["item_counts"])
            value["collab_tool_counts"] = dict(self.progress["collab_tool_counts"])
            value["native_agent_status_counts"] = dict(
                self.progress["native_agent_status_counts"]
            )
            value["progress_sha256"] = _progress_sha256(value)
            validate_codex_progress(value)
            text = json.dumps(value, indent=2, sort_keys=True) + "\n"
            if len(text.encode("utf-8")) > MAX_CODEX_PROGRESS_BYTES:
                raise ValidationError("Codex progress exceeds its byte limit")
            parent_fd = ensure_dir_no_follow(self.path.parent, "Codex progress parent")
            os.close(parent_fd)
            replace_text_file_no_follow(
                self.path,
                "Codex progress",
                text,
                ".codex-progress-",
                sync=False,
            )
        except (OSError, ValidationError):
            self._disabled = True
            return
        self.progress = value
        self._last_write_monotonic = now


def codex_step_progress_path(run, step_id: str) -> Path:
    path = run.resolve_artifact_path(_step_progress_relative(step_id))
    reject_symlink_path(path, "Codex step progress")
    return path


def codex_step_progress_path_from_run_dir(run_dir: Path, step_id: str) -> Path:
    path = Path(run_dir) / "artifacts" / _step_progress_relative(step_id)
    reject_symlink_path(path, "Codex step progress")
    return path


def codex_packet_progress_path(run, step_id: str, packet_index: int) -> Path:
    path = run.resolve_artifact_path(_packet_progress_relative(step_id, packet_index))
    reject_symlink_path(path, "Codex packet progress")
    return path


def codex_packet_progress_dir_from_run_dir(run_dir: Path, step_id: str) -> Path:
    relative = ".codex-progress/%s" % _step_hash(step_id)
    require_no_path_escape(relative)
    path = Path(run_dir) / "artifacts" / relative
    reject_symlink_path(path, "Codex packet progress directory")
    return path


def list_codex_packet_progress(run_dir: Path, step_id: str, *, limit: int = 128) -> List[Dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
        raise ValidationError("Codex packet progress list limit is invalid")
    directory = codex_packet_progress_dir_from_run_dir(run_dir, step_id)
    if not directory.exists() and not directory.is_symlink():
        return []
    directory_fd = open_dir_no_follow(directory, "Codex packet progress directory")
    try:
        names = sorted(os.listdir(directory_fd))
    finally:
        os.close(directory_fd)
    results = []
    for name in names:
        match = re.fullmatch(r"packet-([0-9]{6})\.json", name)
        if match is None:
            raise ValidationError("Codex packet progress directory contains an unknown entry")
        packet_index = int(match.group(1))
        path = directory / name
        progress = load_codex_progress(path)
        if (
            progress["scope"] != "packet"
            or progress["step_id"] != step_id
            or progress["packet_index"] != packet_index
        ):
            raise ValidationError("Codex packet progress path binding changed")
        results.append(codex_progress_summary(progress))
    results.sort(key=lambda value: (value["status"] != "active", -value["packet_index"]))
    return results[:limit]


def load_codex_progress(path: Path, **expected) -> Dict:
    reject_symlink_path(path, "Codex progress")
    text = read_regular_text_file_no_follow(path, "Codex progress", MAX_CODEX_PROGRESS_BYTES)
    try:
        progress = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError("Codex progress is invalid JSON: %s" % exc.__class__.__name__)
    validate_codex_progress(progress, **expected)
    return progress


def validate_codex_progress(
    progress: Dict,
    *,
    workflow_fingerprint: Optional[str] = None,
    step_id: Optional[str] = None,
    scope: Optional[str] = None,
    packet_index: Optional[int] = None,
) -> None:
    if not isinstance(progress, dict):
        raise ValidationError("Codex progress has invalid fields")
    schema = progress.get("schema")
    if schema == CODEX_PROGRESS_SCHEMA:
        expected_fields = PROGRESS_FIELDS
        item_count_fields = ITEM_COUNT_FIELDS
        last_events = LAST_EVENTS
    elif schema == CODEX_PROGRESS_SCHEMA_V3:
        expected_fields = PROGRESS_FIELDS_V3
        item_count_fields = ITEM_COUNT_FIELDS
        last_events = LAST_EVENTS
    elif schema == CODEX_PROGRESS_SCHEMA_V2:
        expected_fields = PROGRESS_FIELDS_V2
        item_count_fields = ITEM_COUNT_FIELDS
        last_events = LAST_EVENTS
    elif schema == CODEX_PROGRESS_SCHEMA_V1:
        expected_fields = PROGRESS_FIELDS_V1
        item_count_fields = ITEM_COUNT_FIELDS_V1
        last_events = LAST_EVENTS_V1
    else:
        raise ValidationError("Codex progress schema is invalid")
    if set(progress) != expected_fields:
        raise ValidationError("Codex progress has invalid fields")
    if progress.get("status") not in PROGRESS_STATUSES:
        raise ValidationError("Codex progress status is invalid")
    if progress.get("scope") not in {"step", "packet"}:
        raise ValidationError("Codex progress scope is invalid")
    _validate_id(progress.get("step_id"), "Codex progress step_id")
    _validate_sha256(progress.get("workflow_fingerprint"), "Codex progress workflow fingerprint")
    _validate_uuid(progress.get("invocation_id"), "Codex progress invocation id")
    _validate_int(progress.get("attempt"), 1, MAX_CODEX_PROGRESS_ATTEMPTS, "attempt")
    for field in ("started_at_utc", "updated_at_utc"):
        _validate_timestamp(progress.get(field), "Codex progress %s" % field)
    if _timestamp_value(progress["started_at_utc"]) > _timestamp_value(progress["updated_at_utc"]):
        raise ValidationError("Codex progress timestamps are inconsistent")
    finished = progress.get("finished_at_utc")
    if progress["status"] == "active":
        if finished is not None:
            raise ValidationError("active Codex progress has a finish timestamp")
    else:
        _validate_timestamp(finished, "Codex progress finish timestamp")
        if _timestamp_value(progress["updated_at_utc"]) > _timestamp_value(finished):
            raise ValidationError("Codex progress terminal timestamps are inconsistent")
    if progress["scope"] == "step":
        if progress.get("packet_index") is not None:
            raise ValidationError("step Codex progress has a packet index")
    else:
        _validate_int(
            progress.get("packet_index"),
            1,
            MAX_CODEX_PROGRESS_PACKET_INDEX,
            "packet_index",
        )
    if progress.get("sandbox") not in {"read-only", "workspace-write"}:
        raise ValidationError("Codex progress sandbox is invalid")
    for field in ("model", "effort"):
        value = progress.get(field)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            raise ValidationError("Codex progress %s is invalid" % field)
    max_tokens = progress.get("max_tokens")
    if max_tokens is not None:
        _validate_int(max_tokens, 100, MAX_PROVIDER_TOKENS, "max_tokens")
    session_hash = progress.get("session_id_sha256")
    if session_hash is not None:
        _validate_sha256(session_hash, "Codex progress session hash")
    for field in (
        "event_count",
        "turn_started_count",
        "turn_completed_count",
        "item_started_count",
        "item_completed_count",
        "failed_item_count",
    ):
        _validate_int(progress.get(field), 0, MAX_CODEX_PROGRESS_EVENTS, field)
    if not isinstance(progress.get("events_truncated"), bool):
        raise ValidationError("Codex progress events_truncated is invalid")
    counts = progress.get("item_counts")
    if not isinstance(counts, dict) or set(counts) != item_count_fields:
        raise ValidationError("Codex progress item counts are invalid")
    for field, value in counts.items():
        _validate_int(value, 0, MAX_CODEX_PROGRESS_EVENTS, "item_counts.%s" % field)
    if sum(counts.values()) != progress["item_completed_count"]:
        raise ValidationError("Codex progress item counts do not match completed items")
    if progress["failed_item_count"] > progress["item_completed_count"]:
        raise ValidationError("Codex progress failed item count is invalid")
    if schema in {
        CODEX_PROGRESS_SCHEMA,
        CODEX_PROGRESS_SCHEMA_V3,
        CODEX_PROGRESS_SCHEMA_V2,
    }:
        _validate_native_agent_progress(
            progress,
            allow_attribution=schema == CODEX_PROGRESS_SCHEMA,
        )
    if schema in {CODEX_PROGRESS_SCHEMA, CODEX_PROGRESS_SCHEMA_V3}:
        _validate_native_agent_authority(progress)
    if schema == CODEX_PROGRESS_SCHEMA:
        _validate_native_agent_reconciliation(progress)
    if progress.get("last_event") not in last_events:
        raise ValidationError("Codex progress last event is invalid")
    _validate_usage(progress)
    _validate_sha256(progress.get("progress_sha256"), "Codex progress hash")
    if progress["progress_sha256"] != _progress_sha256(progress):
        raise ValidationError("Codex progress hash changed")
    if workflow_fingerprint is not None and progress["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("Codex progress workflow binding changed")
    if step_id is not None and progress["step_id"] != step_id:
        raise ValidationError("Codex progress step binding changed")
    if scope is not None and progress["scope"] != scope:
        raise ValidationError("Codex progress scope binding changed")
    if packet_index is not None and progress["packet_index"] != packet_index:
        raise ValidationError("Codex progress packet binding changed")


def codex_progress_summary(progress: Dict) -> Dict:
    validate_codex_progress(progress)
    if progress["schema"] in {
        CODEX_PROGRESS_SCHEMA,
        CODEX_PROGRESS_SCHEMA_V3,
        CODEX_PROGRESS_SCHEMA_V2,
    }:
        collab_tool_counts = dict(progress["collab_tool_counts"])
        collab_tool_failed_count = progress["collab_tool_failed_count"]
        native_agent_count = progress["native_agent_count"]
        native_agent_status_counts = dict(progress["native_agent_status_counts"])
        native_agent_observation_truncated = progress[
            "native_agent_observation_truncated"
        ]
        native_agent_usage_attributed = progress["native_agent_usage_attributed"]
        native_agents_enabled_by_observer = progress[
            "native_agents_enabled_by_observer"
        ]
    else:
        collab_tool_counts = {
            name: 0 for name in sorted(COLLAB_TOOL_COUNT_FIELDS)
        }
        collab_tool_failed_count = 0
        native_agent_count = 0
        native_agent_status_counts = {
            name: 0 for name in sorted(NATIVE_AGENT_STATUS_FIELDS)
        }
        native_agent_observation_truncated = False
        native_agent_usage_attributed = False
        native_agents_enabled_by_observer = False
    if progress["schema"] in {CODEX_PROGRESS_SCHEMA, CODEX_PROGRESS_SCHEMA_V3}:
        native_agents_enabled_by_runner = progress["native_agents_enabled_by_runner"]
        native_agent_max_threads = progress["native_agent_max_threads"]
        native_agent_max_depth = progress["native_agent_max_depth"]
    else:
        native_agents_enabled_by_runner = False
        native_agent_max_threads = None
        native_agent_max_depth = None
    if progress["schema"] == CODEX_PROGRESS_SCHEMA:
        native_agent_usage_status = progress["native_agent_usage_status"]
        native_agent_usage_session_count = progress[
            "native_agent_usage_session_count"
        ]
        native_agent_usage_child_count = progress[
            "native_agent_usage_child_count"
        ]
        native_agent_usage_input_tokens = progress[
            "native_agent_usage_input_tokens"
        ]
        native_agent_usage_cached_input_tokens = progress[
            "native_agent_usage_cached_input_tokens"
        ]
        native_agent_usage_output_tokens = progress[
            "native_agent_usage_output_tokens"
        ]
        native_agent_usage_total_tokens = progress[
            "native_agent_usage_total_tokens"
        ]
        native_agent_usage_rollout_tokens = progress[
            "native_agent_usage_rollout_tokens"
        ]
    else:
        native_agent_usage_status = (
            "unavailable"
            if native_agents_enabled_by_runner or native_agent_count
            else "not-requested"
        )
        native_agent_usage_session_count = 0
        native_agent_usage_child_count = 0
        native_agent_usage_input_tokens = None
        native_agent_usage_cached_input_tokens = None
        native_agent_usage_output_tokens = None
        native_agent_usage_total_tokens = None
        native_agent_usage_rollout_tokens = None
    return {
        "schema": progress["schema"],
        "progress_sha256": progress["progress_sha256"],
        "status": progress["status"],
        "scope": progress["scope"],
        "step_id": progress["step_id"],
        "packet_index": progress["packet_index"],
        "workflow_fingerprint": progress["workflow_fingerprint"],
        "attempt": progress["attempt"],
        "started_at_utc": progress["started_at_utc"],
        "updated_at_utc": progress["updated_at_utc"],
        "finished_at_utc": progress["finished_at_utc"],
        "sandbox": progress["sandbox"],
        "model": progress["model"],
        "effort": progress["effort"],
        "max_tokens": progress["max_tokens"],
        "session_id_sha256": progress["session_id_sha256"],
        "event_count": progress["event_count"],
        "events_truncated": progress["events_truncated"],
        "turn_started_count": progress["turn_started_count"],
        "turn_completed_count": progress["turn_completed_count"],
        "item_started_count": progress["item_started_count"],
        "item_completed_count": progress["item_completed_count"],
        "failed_item_count": progress["failed_item_count"],
        "active_item_count": max(
            0,
            progress["item_started_count"] - progress["item_completed_count"],
        ),
        "item_counts": dict(progress["item_counts"]),
        "collab_tool_call_count": sum(collab_tool_counts.values()),
        "collab_tool_counts": collab_tool_counts,
        "collab_tool_failed_count": collab_tool_failed_count,
        "native_agent_count": native_agent_count,
        "native_agent_active_count": sum(
            native_agent_status_counts[name]
            for name in NATIVE_AGENT_ACTIVE_STATUSES
        ),
        "native_agent_error_count": sum(
            native_agent_status_counts[name]
            for name in NATIVE_AGENT_ERROR_STATUSES
        ),
        "native_agent_status_counts": native_agent_status_counts,
        "native_agent_observation_truncated": native_agent_observation_truncated,
        "native_agent_usage_attributed": native_agent_usage_attributed,
        "native_agents_enabled_by_observer": native_agents_enabled_by_observer,
        "native_agents_enabled_by_runner": native_agents_enabled_by_runner,
        "native_agent_max_threads": native_agent_max_threads,
        "native_agent_max_depth": native_agent_max_depth,
        "native_agent_usage_status": native_agent_usage_status,
        "native_agent_usage_session_count": native_agent_usage_session_count,
        "native_agent_usage_child_count": native_agent_usage_child_count,
        "native_agent_usage_input_tokens": native_agent_usage_input_tokens,
        "native_agent_usage_cached_input_tokens": native_agent_usage_cached_input_tokens,
        "native_agent_usage_output_tokens": native_agent_usage_output_tokens,
        "native_agent_usage_total_tokens": native_agent_usage_total_tokens,
        "native_agent_usage_rollout_tokens": native_agent_usage_rollout_tokens,
        "last_event": progress["last_event"],
        "input_tokens": progress["input_tokens"],
        "output_tokens": progress["output_tokens"],
        "total_tokens": progress["total_tokens"],
        "raw_provider_content_persisted": False,
    }


def _step_progress_relative(step_id: str) -> str:
    relative = ".codex-progress/%s/step.json" % _step_hash(step_id)
    require_no_path_escape(relative)
    return relative


def _packet_progress_relative(step_id: str, packet_index: int) -> str:
    _validate_int(packet_index, 1, MAX_CODEX_PROGRESS_PACKET_INDEX, "packet_index")
    relative = ".codex-progress/%s/packet-%06d.json" % (_step_hash(step_id), packet_index)
    require_no_path_escape(relative)
    return relative


def _step_hash(step_id: str) -> str:
    _validate_id(step_id, "Codex progress step_id")
    return hashlib.sha256(step_id.encode("utf-8")).hexdigest()


def _item_kind(value) -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    if name in ITEM_COUNT_FIELDS:
        return name
    if name in {"mcp_tool_call", "function_call", "dynamic_tool_call", "tool_call"}:
        return "tool_call"
    if name in {"todo_list", "task_list"}:
        return "plan"
    return "other"


def _collab_tool(value) -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    return name if name in COLLAB_TOOL_COUNT_FIELDS else "other"


def _native_agent_status(value) -> str:
    raw = value.get("status") if isinstance(value, dict) else value
    name = str(raw or "").strip().lower().replace("-", "_")
    return name if name in NATIVE_AGENT_STATUS_FIELDS else "unknown"


def _validate_native_agent_progress(
    progress: Dict,
    *,
    allow_attribution: bool,
) -> None:
    collab_counts = progress.get("collab_tool_counts")
    if not isinstance(collab_counts, dict) or set(collab_counts) != COLLAB_TOOL_COUNT_FIELDS:
        raise ValidationError("Codex progress collaboration tool counts are invalid")
    for field, value in collab_counts.items():
        _validate_int(
            value,
            0,
            MAX_CODEX_PROGRESS_EVENTS,
            "collab_tool_counts.%s" % field,
        )
    if sum(collab_counts.values()) != progress["item_counts"]["collab_tool_call"]:
        raise ValidationError("Codex progress collaboration tool counts do not match items")
    _validate_int(
        progress.get("collab_tool_failed_count"),
        0,
        MAX_CODEX_PROGRESS_EVENTS,
        "collab_tool_failed_count",
    )
    if progress["collab_tool_failed_count"] > sum(collab_counts.values()):
        raise ValidationError("Codex progress collaboration failure count is invalid")

    _validate_int(
        progress.get("native_agent_count"),
        0,
        MAX_CODEX_PROGRESS_NATIVE_AGENTS,
        "native_agent_count",
    )
    status_counts = progress.get("native_agent_status_counts")
    if not isinstance(status_counts, dict) or set(status_counts) != NATIVE_AGENT_STATUS_FIELDS:
        raise ValidationError("Codex progress native agent status counts are invalid")
    for field, value in status_counts.items():
        _validate_int(
            value,
            0,
            MAX_CODEX_PROGRESS_NATIVE_AGENTS,
            "native_agent_status_counts.%s" % field,
        )
    if sum(status_counts.values()) != progress["native_agent_count"]:
        raise ValidationError("Codex progress native agent status counts do not match agents")
    if not isinstance(progress.get("native_agent_observation_truncated"), bool):
        raise ValidationError("Codex progress native agent truncation marker is invalid")
    if allow_attribution:
        if not isinstance(progress.get("native_agent_usage_attributed"), bool):
            raise ValidationError("Codex progress native agent usage attribution is invalid")
    elif progress.get("native_agent_usage_attributed") is not False:
        raise ValidationError("legacy Codex progress cannot claim native usage attribution")
    if progress.get("native_agents_enabled_by_observer") is not False:
        raise ValidationError("Codex progress observer enablement marker is invalid")


def _validate_native_agent_authority(progress: Dict) -> None:
    enabled = progress.get("native_agents_enabled_by_runner")
    if not isinstance(enabled, bool):
        raise ValidationError("Codex progress runner native-agent enablement is invalid")
    max_threads = progress.get("native_agent_max_threads")
    max_depth = progress.get("native_agent_max_depth")
    if not enabled:
        if max_threads is not None or max_depth is not None:
            raise ValidationError("disabled Codex progress native-agent limits must be null")
        return
    _validate_int(
        max_threads,
        MIN_CODEX_NATIVE_AGENT_THREADS,
        MAX_CODEX_NATIVE_AGENT_THREADS,
        "native_agent_max_threads",
    )
    if max_depth != CODEX_NATIVE_AGENT_MAX_DEPTH:
        raise ValidationError("Codex progress native-agent depth is invalid")
    if progress.get("scope") != "step":
        raise ValidationError("Codex packet progress cannot enable native agents")


def _validate_native_agent_reconciliation(progress: Dict) -> None:
    status_value = progress.get("native_agent_usage_status")
    if status_value not in NATIVE_USAGE_STATUSES | {"pending"}:
        raise ValidationError("Codex progress native usage status is invalid")
    usage = {
        "status": status_value if status_value != "pending" else "unavailable",
        "session_count": progress.get("native_agent_usage_session_count"),
        "child_count": progress.get("native_agent_usage_child_count"),
        "input_tokens": progress.get("native_agent_usage_input_tokens"),
        "cached_input_tokens": progress.get(
            "native_agent_usage_cached_input_tokens"
        ),
        "output_tokens": progress.get("native_agent_usage_output_tokens"),
        "total_tokens": progress.get("native_agent_usage_total_tokens"),
        "rollout_tokens": progress.get("native_agent_usage_rollout_tokens"),
    }
    validate_native_usage(usage)
    enabled = progress["native_agents_enabled_by_runner"]
    attributed = progress["native_agent_usage_attributed"]
    if not enabled:
        if status_value != "not-requested" or attributed:
            raise ValidationError("disabled Codex progress native usage is inconsistent")
        return
    if status_value == "not-requested":
        raise ValidationError("enabled Codex progress must reconcile native usage")
    if attributed != (status_value == "complete"):
        raise ValidationError("Codex progress native usage attribution is inconsistent")


def _validate_usage(progress: Dict) -> None:
    values = [progress.get(field) for field in ("input_tokens", "output_tokens", "total_tokens")]
    for field, value in zip(("input_tokens", "output_tokens", "total_tokens"), values):
        if value is not None:
            _validate_int(value, 0, MAX_PROVIDER_TOKENS, field)
    input_tokens, output_tokens, total_tokens = values
    if input_tokens is not None and output_tokens is not None:
        if total_tokens != input_tokens + output_tokens:
            raise ValidationError("Codex progress token totals are inconsistent")


def _progress_sha256(progress: Dict) -> str:
    value = dict(progress)
    value.pop("progress_sha256", None)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return str(parsed) == value


def _validate_uuid(value, label: str) -> None:
    if not _canonical_uuid(value):
        raise ValidationError("%s is invalid" % label)


def _validate_id(value, label: str) -> None:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise ValidationError("%s is invalid" % label)


def _validate_sha256(value, label: str) -> None:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValidationError("%s is invalid" % label)


def _validate_int(value, minimum: int, maximum: int, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError("Codex progress %s is invalid" % label)


def _validate_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("%s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValidationError("%s is invalid" % label)


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value):
    try:
        number = float(value)
    except ValueError:
        raise ValueError("invalid JSON constant")
    if not math.isfinite(number):
        raise ValueError("invalid JSON constant")
    return number
