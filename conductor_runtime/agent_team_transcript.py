import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .clock import utc_now
from .errors import ValidationError
from .redaction import redact_text
from .security import (
    ensure_dir_no_follow,
    read_regular_text_file_no_follow,
    reject_symlink_path,
    replace_text_file_no_follow,
    require_no_path_escape,
    write_new_text_file_no_follow,
)


AGENT_TEAM_TRANSCRIPT_SCHEMA = "conductor.agent_team_transcript.v1"
AGENT_TEAM_TRANSCRIPT_POLICY_FIELDS = {"max_events", "max_bytes"}
AGENT_TEAM_TRANSCRIPT_FIELDS = {
    "schema",
    "transcript_id",
    "step_id",
    "workflow_fingerprint",
    "generation",
    "round",
    "member_id",
    "task_id",
    "attempt",
    "status",
    "created_at_utc",
    "updated_at_utc",
    "max_events",
    "max_bytes",
    "provider_lines",
    "ignored_lines",
    "events_dropped",
    "truncated",
    "session_id_sha256",
    "output_sha256",
    "error_class",
    "events",
}
AGENT_TEAM_TRANSCRIPT_EVENT_FIELDS = {
    "sequence",
    "at_utc",
    "type",
    "item_type",
    "status",
    "text",
    "text_sha256",
    "truncated",
}
AGENT_TEAM_TRANSCRIPT_STATUSES = {
    "active",
    "completed",
    "interrupted",
    "failed",
    "timed-out",
}
AGENT_TEAM_TRANSCRIPT_EVENT_TYPES = {
    "session-started",
    "turn-started",
    "activity",
    "assistant-message",
    "turn-completed",
    "provider-error",
}
AGENT_TEAM_TRANSCRIPT_ACTIVITY_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
    "collab_tool_call",
}
MIN_AGENT_TEAM_TRANSCRIPT_EVENTS = 8
MAX_AGENT_TEAM_TRANSCRIPT_EVENTS = 1024
MIN_AGENT_TEAM_TRANSCRIPT_BYTES = 4 * 1024
MAX_AGENT_TEAM_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_AGENT_TEAM_TRANSCRIPT_MESSAGE_CHARS = 16_000
MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES = 1_000_000
MAX_AGENT_TEAM_TRANSCRIPT_ATTEMPT = 99

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_TRANSCRIPT_ID = re.compile(r"^transcript-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CLASS = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{0,127}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$"
)


def validate_agent_team_transcript_policy(policy: Dict, step_id: str) -> None:
    if not isinstance(policy, dict) or set(policy) != AGENT_TEAM_TRANSCRIPT_POLICY_FIELDS:
        raise ValidationError("agent_team step %s operator_console has invalid fields" % step_id)
    _bounded_int(
        policy.get("max_events"),
        MIN_AGENT_TEAM_TRANSCRIPT_EVENTS,
        MAX_AGENT_TEAM_TRANSCRIPT_EVENTS,
        "agent_team step %s operator_console max_events" % step_id,
    )
    _bounded_int(
        policy.get("max_bytes"),
        MIN_AGENT_TEAM_TRANSCRIPT_BYTES,
        MAX_AGENT_TEAM_TRANSCRIPT_BYTES,
        "agent_team step %s operator_console max_bytes" % step_id,
    )


def agent_team_transcript_id(
    workflow_fingerprint: str,
    step_id: str,
    generation: int,
    round_number: int,
    member_id: str,
    task_id: str,
    attempt: int,
) -> str:
    material = "\0".join(
        [
            workflow_fingerprint,
            step_id,
            str(generation),
            str(round_number),
            member_id,
            task_id,
            str(attempt),
        ]
    )
    return "transcript-%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def agent_team_transcript_root(run, step: Dict, generation: int) -> Path:
    return agent_team_transcript_root_from_artifacts(
        run.artifacts_dir,
        step,
        generation,
    )


def agent_team_transcript_root_from_artifacts(
    artifacts_dir: Path,
    step: Dict,
    generation: int,
) -> Path:
    relative = "%s/operator-console/generation-%09d" % (
        step["capture_dir"],
        generation,
    )
    require_no_path_escape(relative)
    root = Path(artifacts_dir) / relative
    reject_symlink_path(root, "agent team transcript directory")
    return root


def agent_team_transcript_path(run, step: Dict, generation: int, transcript_id: str) -> Path:
    return agent_team_transcript_path_from_artifacts(
        run.artifacts_dir,
        step,
        generation,
        transcript_id,
    )


def agent_team_transcript_path_from_artifacts(
    artifacts_dir: Path,
    step: Dict,
    generation: int,
    transcript_id: str,
) -> Path:
    if not isinstance(transcript_id, str) or not _TRANSCRIPT_ID.fullmatch(transcript_id):
        raise ValidationError("agent team transcript id is invalid")
    path = agent_team_transcript_root_from_artifacts(
        artifacts_dir,
        step,
        generation,
    ) / (transcript_id + ".json")
    reject_symlink_path(path, "agent team transcript")
    return path


def load_bound_agent_team_transcript(
    artifacts_dir: Path,
    step: Dict,
    workflow_fingerprint: str,
    generation: int,
    transcript_id: str,
) -> Dict:
    path = agent_team_transcript_path_from_artifacts(
        artifacts_dir,
        step,
        generation,
        transcript_id,
    )
    value = load_agent_team_transcript(path)
    verify_agent_team_transcript(
        value,
        step=step,
        workflow_fingerprint=workflow_fingerprint,
        generation=generation,
    )
    return value


def list_agent_team_transcript_summaries(
    artifacts_dir: Path,
    step: Dict,
    workflow_fingerprint: str,
    generation: int,
) -> list:
    root = agent_team_transcript_root_from_artifacts(
        artifacts_dir,
        step,
        generation,
    )
    values = []
    if not (root.exists() or root.is_symlink()):
        return values
    reject_symlink_path(root, "agent team transcript directory")
    if not root.is_dir():
        raise ValidationError("agent team transcript directory must be a directory")
    try:
        with os.scandir(root) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
    except OSError as exc:
        raise ValidationError(
            "failed to scan agent team transcripts: %s" % exc.__class__.__name__
        )
    for entry in entries:
        if entry.name.startswith(".agent-team-transcript-"):
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValidationError(
                    "failed to stat agent team transcript temporary file: %s"
                    % exc.__class__.__name__
                )
            if not stat.S_ISREG(info.st_mode):
                raise ValidationError(
                    "agent team transcript directory contains a non-file entry"
                )
            continue
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise ValidationError(
                "agent team transcript directory contains a non-file entry"
            )
        if not entry.name.startswith("transcript-") or not entry.name.endswith(".json"):
            raise ValidationError(
                "agent team transcript directory contains an unknown file"
            )
        value = load_agent_team_transcript(Path(entry.path))
        verify_agent_team_transcript(
            value,
            step=step,
            workflow_fingerprint=workflow_fingerprint,
            generation=generation,
        )
        expected_path = agent_team_transcript_path_from_artifacts(
            artifacts_dir,
            step,
            generation,
            value["transcript_id"],
        )
        if expected_path.name != entry.name:
            raise ValidationError("agent team transcript filename binding changed")
        normalized = json.dumps(value, sort_keys=True, ensure_ascii=True)
        values.append(
            agent_team_transcript_summary(
                value,
                file_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            )
        )
    values.sort(
        key=lambda value: (
            value["round"],
            value["member_id"],
            value["task_id"],
            value["attempt"],
            value["transcript_id"],
        )
    )
    return values


def load_agent_team_transcript(path: Path) -> Dict:
    try:
        value = json.loads(
            read_regular_text_file_no_follow(
                path,
                "agent team transcript",
                MAX_AGENT_TEAM_TRANSCRIPT_BYTES,
            )
        )
    except json.JSONDecodeError as exc:
        raise ValidationError("agent team transcript is not valid JSON: %s" % exc)
    validate_agent_team_transcript(value)
    return value


def validate_agent_team_transcript(value: Dict) -> None:
    if not isinstance(value, dict) or set(value) != AGENT_TEAM_TRANSCRIPT_FIELDS:
        raise ValidationError("agent team transcript has invalid fields")
    if value.get("schema") != AGENT_TEAM_TRANSCRIPT_SCHEMA:
        raise ValidationError("agent team transcript has an unsupported schema")
    if not _TRANSCRIPT_ID.fullmatch(str(value.get("transcript_id") or "")):
        raise ValidationError("agent team transcript id is invalid")
    for field in ("step_id", "member_id", "task_id"):
        if not isinstance(value.get(field), str) or not _SAFE_ID.fullmatch(value[field]):
            raise ValidationError("agent team transcript %s is invalid" % field)
    _require_sha256(value.get("workflow_fingerprint"), "workflow_fingerprint")
    _bounded_int(value.get("generation"), 0, 10**9, "agent team transcript generation")
    _bounded_int(value.get("round"), 1, 16, "agent team transcript round")
    _bounded_int(
        value.get("attempt"),
        0,
        MAX_AGENT_TEAM_TRANSCRIPT_ATTEMPT,
        "agent team transcript attempt",
    )
    if value.get("status") not in AGENT_TEAM_TRANSCRIPT_STATUSES:
        raise ValidationError("agent team transcript status is invalid")
    _require_timestamp(value.get("created_at_utc"), "created_at_utc")
    _require_timestamp(value.get("updated_at_utc"), "updated_at_utc")
    if value["updated_at_utc"] < value["created_at_utc"]:
        raise ValidationError("agent team transcript timestamps are out of order")
    _bounded_int(
        value.get("max_events"),
        MIN_AGENT_TEAM_TRANSCRIPT_EVENTS,
        MAX_AGENT_TEAM_TRANSCRIPT_EVENTS,
        "agent team transcript max_events",
    )
    _bounded_int(
        value.get("max_bytes"),
        MIN_AGENT_TEAM_TRANSCRIPT_BYTES,
        MAX_AGENT_TEAM_TRANSCRIPT_BYTES,
        "agent team transcript max_bytes",
    )
    _bounded_int(
        value.get("provider_lines"),
        0,
        MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES,
        "agent team transcript provider_lines",
    )
    _bounded_int(
        value.get("ignored_lines"),
        0,
        value["provider_lines"],
        "agent team transcript ignored_lines",
    )
    _bounded_int(
        value.get("events_dropped"),
        0,
        MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES,
        "agent team transcript events_dropped",
    )
    if not isinstance(value.get("truncated"), bool):
        raise ValidationError("agent team transcript truncated must be boolean")
    for field in ("session_id_sha256", "output_sha256"):
        if value.get(field) is not None:
            _require_sha256(value[field], field)
    error_class = value.get("error_class")
    if error_class is not None and (
        not isinstance(error_class, str) or not _ERROR_CLASS.fullmatch(error_class)
    ):
        raise ValidationError("agent team transcript error_class is invalid")
    if value["status"] == "active" and (value["output_sha256"] is not None or error_class is not None):
        raise ValidationError("active agent team transcript cannot contain terminal evidence")
    if value["status"] in {"completed", "interrupted"} and (
        value["output_sha256"] is None
        or value["session_id_sha256"] is None
        or error_class is not None
    ):
        raise ValidationError("successful agent team transcript terminal evidence is incomplete")
    if value["status"] in {"failed", "timed-out"} and (
        value["output_sha256"] is not None or error_class is None
    ):
        raise ValidationError("failed agent team transcript terminal evidence is incomplete")
    events = value.get("events")
    if not isinstance(events, list) or len(events) > value["max_events"]:
        raise ValidationError("agent team transcript events exceed the configured limit")
    for index, event in enumerate(events, 1):
        _validate_event(event, index)
    if value["provider_lines"] != (
        value["ignored_lines"] + value["events_dropped"] + len(events)
    ):
        raise ValidationError("agent team transcript provider line accounting is inconsistent")
    if value["session_id_sha256"] is not None and not any(
        event["type"] == "session-started" for event in events
    ):
        raise ValidationError("agent team transcript session event is missing")
    if value["truncated"] != bool(
        value["events_dropped"]
        or any(event["truncated"] for event in events)
    ):
        raise ValidationError("agent team transcript truncation evidence is inconsistent")
    if len(_serialize(value).encode("utf-8")) > value["max_bytes"]:
        raise ValidationError("agent team transcript exceeds its configured byte limit")


def verify_agent_team_transcript(
    value: Dict,
    *,
    step: Dict,
    workflow_fingerprint: str,
    generation: int,
) -> None:
    validate_agent_team_transcript(value)
    if step.get("operator_console") is None:
        raise ValidationError("agent team transcript is not enabled for this step")
    validate_agent_team_transcript_policy(step["operator_console"], step["id"])
    if value["step_id"] != step["id"]:
        raise ValidationError("agent team transcript step binding changed")
    if value["workflow_fingerprint"] != workflow_fingerprint:
        raise ValidationError("agent team transcript workflow binding changed")
    if value["generation"] != generation:
        raise ValidationError("agent team transcript generation changed")
    if value["max_events"] != step["operator_console"]["max_events"]:
        raise ValidationError("agent team transcript event limit changed")
    if value["max_bytes"] != step["operator_console"]["max_bytes"]:
        raise ValidationError("agent team transcript byte limit changed")
    expected_id = agent_team_transcript_id(
        value["workflow_fingerprint"],
        value["step_id"],
        value["generation"],
        value["round"],
        value["member_id"],
        value["task_id"],
        value["attempt"],
    )
    if value["transcript_id"] != expected_id:
        raise ValidationError("agent team transcript identity changed")


def agent_team_transcript_summary(value: Dict, file_sha256: Optional[str] = None) -> Dict:
    validate_agent_team_transcript(value)
    if file_sha256 is not None:
        _require_sha256(file_sha256, "file_sha256")
    return {
        "transcript_id": value["transcript_id"],
        "step_id": value["step_id"],
        "generation": value["generation"],
        "round": value["round"],
        "member_id": value["member_id"],
        "task_id": value["task_id"],
        "attempt": value["attempt"],
        "status": value["status"],
        "updated_at_utc": value["updated_at_utc"],
        "event_count": len(value["events"]),
        "assistant_message_count": sum(
            event["type"] == "assistant-message" for event in value["events"]
        ),
        "events_dropped": value["events_dropped"],
        "truncated": value["truncated"],
        "session_id_sha256": value["session_id_sha256"],
        "output_sha256": value["output_sha256"],
        "file_sha256": file_sha256,
    }


class AgentTeamTranscriptWriter:
    def __init__(
        self,
        *,
        run,
        step: Dict,
        workflow_fingerprint: str,
        generation: int,
        round_number: int,
        member_id: str,
        task_id: str,
        attempt: int,
    ):
        policy = step.get("operator_console")
        validate_agent_team_transcript_policy(policy, step["id"])
        transcript_id = agent_team_transcript_id(
            workflow_fingerprint,
            step["id"],
            generation,
            round_number,
            member_id,
            task_id,
            attempt,
        )
        self.path = agent_team_transcript_path(run, step, generation, transcript_id)
        timestamp = _utc_now_text()
        self.value = {
            "schema": AGENT_TEAM_TRANSCRIPT_SCHEMA,
            "transcript_id": transcript_id,
            "step_id": step["id"],
            "workflow_fingerprint": workflow_fingerprint,
            "generation": generation,
            "round": round_number,
            "member_id": member_id,
            "task_id": task_id,
            "attempt": attempt,
            "status": "active",
            "created_at_utc": timestamp,
            "updated_at_utc": timestamp,
            "max_events": policy["max_events"],
            "max_bytes": policy["max_bytes"],
            "provider_lines": 0,
            "ignored_lines": 0,
            "events_dropped": 0,
            "truncated": False,
            "session_id_sha256": None,
            "output_sha256": None,
            "error_class": None,
            "events": [],
        }
        validate_agent_team_transcript(self.value)
        parent_fd = ensure_dir_no_follow(self.path.parent, "agent team transcript directory")
        os.close(parent_fd)
        write_new_text_file_no_follow(
            self.path,
            "agent team transcript",
            _serialize(self.value),
        )

    @property
    def observed_line_count(self) -> int:
        return self.value["provider_lines"]

    def observe_line(self, line: str) -> None:
        if self.value["status"] != "active":
            raise ValidationError("terminal agent team transcript received provider output")
        if self.value["provider_lines"] >= MAX_AGENT_TEAM_TRANSCRIPT_PROVIDER_LINES:
            raise ValidationError("agent team transcript provider line limit exceeded")
        self.value["provider_lines"] += 1
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            self.value["ignored_lines"] += 1
            return
        try:
            event = self._safe_event(raw)
        except ValidationError:
            self.value["ignored_lines"] += 1
            raise
        if event is None:
            self.value["ignored_lines"] += 1
            return
        self._append_event(event)

    def observe_remaining_stdout(self, stdout: str) -> None:
        lines = str(stdout or "").splitlines()
        observed = self.observed_line_count
        if observed > len(lines):
            return
        for line in lines[observed:]:
            self.observe_line(line)

    def finish(
        self,
        status: str,
        *,
        output_sha256: Optional[str] = None,
        error_class: Optional[str] = None,
    ) -> None:
        if status not in AGENT_TEAM_TRANSCRIPT_STATUSES - {"active"}:
            raise ValidationError("agent team transcript terminal status is invalid")
        if output_sha256 is not None:
            _require_sha256(output_sha256, "output_sha256")
        if error_class is not None and not _ERROR_CLASS.fullmatch(error_class):
            error_class = "ProviderExecutionError"
        self.value["status"] = status
        self.value["output_sha256"] = output_sha256
        self.value["error_class"] = error_class
        self.value["updated_at_utc"] = _utc_now_text()
        self._write_fitted()

    def _safe_event(self, raw) -> Optional[Dict]:
        if not isinstance(raw, dict):
            return None
        event_type = raw.get("type")
        if event_type == "thread.started":
            session_id = raw.get("thread_id")
            if not isinstance(session_id, str) or not session_id:
                raise ValidationError("agent team transcript session id is invalid")
            digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            existing = self.value["session_id_sha256"]
            if existing is not None and existing != digest:
                raise ValidationError("agent team transcript session identity changed")
            self.value["session_id_sha256"] = digest
            return _event("session-started")
        if event_type == "turn.started":
            return _event("turn-started")
        if event_type == "turn.completed":
            return _event("turn-completed")
        if event_type in {"turn.failed", "error"}:
            return _event("provider-error")
        if event_type not in {"item.started", "item.completed"}:
            return None
        item = raw.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "agent_message" and event_type == "item.completed":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            redacted = redact_text(text).strip()
            was_truncated = len(redacted) > MAX_AGENT_TEAM_TRANSCRIPT_MESSAGE_CHARS
            redacted = redacted[:MAX_AGENT_TEAM_TRANSCRIPT_MESSAGE_CHARS]
            return _event(
                "assistant-message",
                text=redacted,
                text_sha256=hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
                truncated=was_truncated,
            )
        if item_type not in AGENT_TEAM_TRANSCRIPT_ACTIVITY_TYPES:
            return None
        return _event(
            "activity",
            item_type=item_type,
            status="started" if event_type == "item.started" else "completed",
        )

    def _append_event(self, event: Dict) -> None:
        event["sequence"] = len(self.value["events"]) + 1
        event["at_utc"] = _utc_now_text()
        self.value["updated_at_utc"] = event["at_utc"]
        if event["truncated"]:
            self.value["truncated"] = True
        if len(self.value["events"]) >= self.value["max_events"]:
            self.value["events_dropped"] += 1
            self.value["truncated"] = True
            self._write_fitted()
            return
        self.value["events"].append(event)
        if len(_serialize(self.value).encode("utf-8")) > self.value["max_bytes"]:
            self.value["events"].pop()
            self.value["events_dropped"] += 1
            self.value["truncated"] = True
        self._write_fitted()

    def _write_fitted(self) -> None:
        while len(_serialize(self.value).encode("utf-8")) > self.value["max_bytes"]:
            if not self.value["events"]:
                raise ValidationError("agent team transcript metadata exceeds its byte limit")
            self.value["events"].pop()
            self.value["events_dropped"] += 1
            self.value["truncated"] = True
        validate_agent_team_transcript(self.value)
        replace_text_file_no_follow(
            self.path,
            "agent team transcript",
            _serialize(self.value),
            temp_prefix=".agent-team-transcript-",
        )


def _event(
    event_type: str,
    *,
    item_type: Optional[str] = None,
    status: Optional[str] = None,
    text: Optional[str] = None,
    text_sha256: Optional[str] = None,
    truncated: bool = False,
) -> Dict:
    return {
        "sequence": 0,
        "at_utc": _utc_now_text(),
        "type": event_type,
        "item_type": item_type,
        "status": status,
        "text": text,
        "text_sha256": text_sha256,
        "truncated": truncated,
    }


def _validate_event(event: Dict, expected_sequence: int) -> None:
    if not isinstance(event, dict) or set(event) != AGENT_TEAM_TRANSCRIPT_EVENT_FIELDS:
        raise ValidationError("agent team transcript event has invalid fields")
    if event.get("sequence") != expected_sequence:
        raise ValidationError("agent team transcript event sequence is invalid")
    _require_timestamp(event.get("at_utc"), "event at_utc")
    event_type = event.get("type")
    if event_type not in AGENT_TEAM_TRANSCRIPT_EVENT_TYPES:
        raise ValidationError("agent team transcript event type is invalid")
    item_type = event.get("item_type")
    status = event.get("status")
    text = event.get("text")
    text_sha256 = event.get("text_sha256")
    if not isinstance(event.get("truncated"), bool):
        raise ValidationError("agent team transcript event truncated must be boolean")
    if event_type == "activity":
        if item_type not in AGENT_TEAM_TRANSCRIPT_ACTIVITY_TYPES:
            raise ValidationError("agent team transcript activity type is invalid")
        if status not in {"started", "completed"}:
            raise ValidationError("agent team transcript activity status is invalid")
        if text is not None or text_sha256 is not None or event["truncated"]:
            raise ValidationError("agent team transcript activity contains private payload fields")
        return
    if event_type == "assistant-message":
        if item_type is not None or status is not None:
            raise ValidationError("agent team transcript assistant message has activity fields")
        if not isinstance(text, str) or not text or len(text) > MAX_AGENT_TEAM_TRANSCRIPT_MESSAGE_CHARS:
            raise ValidationError("agent team transcript assistant message is invalid")
        _require_sha256(text_sha256, "event text_sha256")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            raise ValidationError("agent team transcript assistant message hash changed")
        return
    if any(value is not None for value in (item_type, status, text, text_sha256)) or event["truncated"]:
        raise ValidationError("agent team transcript lifecycle event has payload fields")


def _serialize(value: Dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _utc_now_text() -> str:
    return utc_now().isoformat(timespec="milliseconds") + "Z"


def _bounded_int(value, minimum: int, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError("%s must be an integer from %d to %d" % (label, minimum, maximum))


def _require_sha256(value, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError("agent team transcript %s is invalid" % label)


def _require_timestamp(value, label: str) -> None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValidationError("agent team transcript %s is invalid" % label)
    try:
        datetime.fromisoformat(value[:-1])
    except ValueError:
        raise ValidationError("agent team transcript %s is invalid" % label)
