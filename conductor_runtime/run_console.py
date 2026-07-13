import json
from typing import Dict

from .errors import ValidationError
from .redaction import redact_terminal_text
from .codex_config import (
    MAX_CODEX_NATIVE_AGENT_THREADS,
    MIN_CODEX_NATIVE_AGENT_THREADS,
)
from .codex_native_usage import validate_native_usage


RUN_CONSOLE_SNAPSHOT_SCHEMA = "conductor.run_console_snapshot.v5"
RUN_CONSOLE_MIN_WIDTH = 60
RUN_CONSOLE_MAX_WIDTH = 240
RUN_CONSOLE_MIN_REFRESH_MS = 100
RUN_CONSOLE_MAX_REFRESH_MS = 5000
RUN_CONSOLE_MAX_TIMEOUT_SECONDS = 24 * 60 * 60
RUN_CONSOLE_MAX_STEPS = 10_000
RUN_CONSOLE_MAX_COUNT = 1_000_000_000
RUN_CONSOLE_MAX_DURATION_MS = 1_000_000_000_000_000
RUN_CONSOLE_ACTIVE_STATUSES = {
    "planning",
    "running",
    "pause_requested",
    "stop_requested",
    "restart_requested",
}
_STEP_FIELDS = {
    "id",
    "status",
    "phase",
    "kind",
    "duration_ms",
    "codex_progress",
    "codex_packet_progress",
    "runtime_budget_exhausted",
    "partial_output_preserved",
    "partial_output_status",
    "partial_output_source",
    "partial_output_sha256",
    "partial_output_bytes",
}
_PARTIAL_OUTPUT_SOURCES = {"agent-messages", "final-message-file"}
_PARTIAL_OUTPUT_STATUSES = {"none", "valid", "invalid"}
_PROGRESS_STATUSES = {
    "active",
    "completed",
    "failed",
    "timed-out",
    "interrupted",
    "invalid",
    "unknown",
}
_PROGRESS_LAST_EVENTS = {
    "launch",
    "thread-started",
    "turn-started",
    "item-started",
    "item-updated",
    "item-completed",
    "turn-completed",
    "turn-failed",
    "error",
    "other",
    "unknown",
}
_DIAGNOSTIC_SEVERITIES = {"ok", "info", "warning", "action_required", "unknown"}
_NATIVE_USAGE_STATUSES = {"complete", "not-requested", "pending", "unavailable"}
_VALID_PROGRESS_FIELDS = {
    "status",
    "last_event",
    "event_count",
    "item_started_count",
    "item_completed_count",
    "failed_item_count",
    "collab_tool_call_count",
    "collab_tool_failed_count",
    "native_agent_count",
    "native_agent_active_count",
    "native_agent_error_count",
    "native_agent_observation_truncated",
    "native_agent_usage_attributed",
    "native_agents_enabled_by_observer",
    "native_agents_enabled_by_runner",
    "native_agent_max_threads",
    "native_agent_max_depth",
    "native_agent_usage_status",
    "native_agent_usage_session_count",
    "native_agent_usage_child_count",
    "native_agent_usage_input_tokens",
    "native_agent_usage_cached_input_tokens",
    "native_agent_usage_output_tokens",
    "native_agent_usage_total_tokens",
    "native_agent_usage_rollout_tokens",
    "total_tokens",
}
_INVALID_PROGRESS_FIELDS = {"status", "error"}
_PACKET_PROGRESS_FIELDS = {
    "count",
    "active",
    "completed",
    "failed",
    "invalid",
    "event_count",
    "item_started_count",
    "item_completed_count",
    "collab_tool_call_count",
    "collab_tool_failed_count",
    "native_agent_count",
    "native_agent_active_count",
    "native_agent_error_count",
    "native_agent_observation_truncated",
}


def build_run_console_snapshot(detail: Dict) -> Dict:
    if not isinstance(detail, dict):
        raise ValidationError("run console detail is invalid")
    steps = []
    for raw in detail.get("steps", []) if isinstance(detail.get("steps"), list) else []:
        if not isinstance(raw, dict):
            continue
        step = {field: raw.get(field) for field in _STEP_FIELDS}
        step["id"] = _text(step.get("id") or "unknown", 128)
        step["status"] = _text(step.get("status") or "unknown", 64)
        step["phase"] = _text(step.get("phase") or "default", 128)
        step["kind"] = _text(step.get("kind") or "", 128)
        step["duration_ms"] = _duration_value(step.get("duration_ms"))
        step["runtime_budget_exhausted"] = step.get("runtime_budget_exhausted") is True
        step["partial_output_status"] = _enum(
            step.get("partial_output_status"),
            _PARTIAL_OUTPUT_STATUSES,
            "invalid" if step.get("partial_output_preserved") is True else "none",
        )
        step["partial_output_preserved"] = step["partial_output_status"] == "valid"
        if step["partial_output_status"] == "valid":
            step["partial_output_source"] = _enum(
                step.get("partial_output_source"),
                _PARTIAL_OUTPUT_SOURCES,
                None,
            )
            step["partial_output_sha256"] = _sha256_or_none(
                step.get("partial_output_sha256")
            )
            step["partial_output_bytes"] = _nullable_non_negative_int(
                step.get("partial_output_bytes")
            )
        else:
            step["partial_output_source"] = None
            step["partial_output_sha256"] = None
            step["partial_output_bytes"] = None
        if not isinstance(step.get("codex_progress"), dict):
            step["codex_progress"] = None
        else:
            step["codex_progress"] = _progress_summary(step["codex_progress"])
        packet_progress = step.get("codex_packet_progress")
        step["codex_packet_progress"] = _packet_progress_summary(packet_progress)
        steps.append(step)
    runner = detail.get("runner") if isinstance(detail.get("runner"), dict) else {}
    diagnostics = detail.get("diagnostics") if isinstance(detail.get("diagnostics"), dict) else {}
    snapshot = {
        "schema": RUN_CONSOLE_SNAPSHOT_SCHEMA,
        "id": _text(detail.get("id") or "unknown", 256),
        "workflow": _text(detail.get("workflow") or "unknown", 256),
        "status": _text(detail.get("status") or "unknown", 64),
        "duration_ms": _duration_value(detail.get("duration_ms")),
        "runner": {
            "status": _text(runner.get("status") or "unavailable", 64),
            "liveness": _text(runner.get("liveness") or "unavailable", 64),
            "active_process_count": _non_negative_int(runner.get("active_process_count")),
        },
        "diagnostics": {
            "severity": _enum(
                diagnostics.get("severity"),
                _DIAGNOSTIC_SEVERITIES,
                "unknown",
            ),
            "summary": _text(diagnostics.get("summary") or "", 300),
            "issue_count": _non_negative_int(
                len(diagnostics.get("issues", []))
                if isinstance(diagnostics.get("issues"), list)
                else 0
            ),
        },
        "steps": steps,
    }
    validate_run_console_snapshot(snapshot)
    return snapshot


def validate_run_console_snapshot(snapshot: Dict) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema",
        "id",
        "workflow",
        "status",
        "duration_ms",
        "runner",
        "diagnostics",
        "steps",
    }:
        raise ValidationError("run console snapshot has invalid fields")
    if snapshot.get("schema") != RUN_CONSOLE_SNAPSHOT_SCHEMA:
        raise ValidationError("run console snapshot schema is invalid")
    for field, limit in (("id", 256), ("workflow", 256), ("status", 64)):
        _validate_safe_text(snapshot.get(field), limit, "run console snapshot %s" % field)
    duration = snapshot.get("duration_ms")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= RUN_CONSOLE_MAX_DURATION_MS
    ):
        raise ValidationError("run console duration is invalid")
    runner = snapshot.get("runner")
    if not isinstance(runner, dict) or set(runner) != {
        "status",
        "liveness",
        "active_process_count",
    }:
        raise ValidationError("run console runner is invalid")
    _validate_safe_text(runner.get("status"), 64, "run console runner status")
    _validate_safe_text(runner.get("liveness"), 64, "run console runner liveness")
    _validate_count(runner.get("active_process_count"), "run console active process count")
    diagnostics = snapshot.get("diagnostics")
    if not isinstance(diagnostics, dict) or set(diagnostics) != {
        "severity",
        "summary",
        "issue_count",
    }:
        raise ValidationError("run console diagnostics are invalid")
    if diagnostics.get("severity") not in _DIAGNOSTIC_SEVERITIES:
        raise ValidationError("run console diagnostic severity is invalid")
    _validate_safe_text(diagnostics.get("summary"), 300, "run console diagnostic summary", allow_empty=True)
    _validate_count(diagnostics.get("issue_count"), "run console diagnostic issue count")
    steps = snapshot.get("steps")
    if not isinstance(steps, list) or len(steps) > RUN_CONSOLE_MAX_STEPS:
        raise ValidationError("run console steps are invalid")
    for step in steps:
        _validate_step(step)
    try:
        json.dumps(snapshot, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationError("run console snapshot is not serializable")


def run_console_is_terminal(snapshot: Dict) -> bool:
    validate_run_console_snapshot(snapshot)
    return snapshot["status"] not in RUN_CONSOLE_ACTIVE_STATUSES


def render_run_console(snapshot: Dict, *, width: int) -> str:
    validate_run_console_snapshot(snapshot)
    if isinstance(width, bool) or not isinstance(width, int) or not RUN_CONSOLE_MIN_WIDTH <= width <= RUN_CONSOLE_MAX_WIDTH:
        raise ValidationError(
            "run console width must be from %d to %d"
            % (RUN_CONSOLE_MIN_WIDTH, RUN_CONSOLE_MAX_WIDTH)
        )
    completed = sum(step["status"] in {"completed", "skipped", "planned"} for step in snapshot["steps"])
    total = len(snapshot["steps"])
    bar_width = min(30, max(10, width - 42))
    filled = int(bar_width * completed / total) if total else 0
    progress_bar = "[" + "#" * filled + "." * (bar_width - filled) + "]"
    lines = [
        _fit("Conductor run %s" % snapshot["id"], width),
        _fit(
            "%s | %s | %s %d/%d"
            % (
                snapshot["workflow"],
                snapshot["status"],
                progress_bar,
                completed,
                total,
            ),
            width,
        ),
        _fit(
            "runner %s/%s | %d active child process(es) | diagnostics %s (%d)"
            % (
                snapshot["runner"]["status"],
                snapshot["runner"]["liveness"],
                snapshot["runner"]["active_process_count"],
                snapshot["diagnostics"]["severity"],
                snapshot["diagnostics"]["issue_count"],
            ),
            width,
        ),
        "-" * width,
    ]
    for step in snapshot["steps"]:
        timing = _duration(step["duration_ms"])
        lines.append(
            _fit(
                "%-12s %-18s %-14s %s%s"
                % (
                    step["status"],
                    step["id"],
                    step["kind"],
                    step["phase"],
                    " | %s" % timing if timing else "",
                ),
                width,
            )
        )
        progress = step.get("codex_progress")
        if isinstance(progress, dict):
            lines.append(_fit("  Codex %s" % _progress_text(progress), width))
        packet = step.get("codex_packet_progress")
        if isinstance(packet, dict) and packet.get("count", 0):
            lines.append(
                _fit(
                    "  Packets %d active / %d completed / %d failed | %d events | %d/%d items"
                    % (
                        packet["active"],
                        packet["completed"],
                        packet["failed"],
                        packet["event_count"],
                        packet["item_completed_count"],
                        packet["item_started_count"],
                    ),
                    width,
                )
            )
            if packet["native_agent_count"] or packet["collab_tool_call_count"]:
                lines.append(
                    _fit(
                        "  Native %d observed / %d active / %d errors | %d collab calls / %d failed | usage unattributed%s"
                        % (
                            packet["native_agent_count"],
                            packet["native_agent_active_count"],
                            packet["native_agent_error_count"],
                            packet["collab_tool_call_count"],
                            packet["collab_tool_failed_count"],
                            (
                                " | observation truncated"
                                if packet["native_agent_observation_truncated"]
                                else ""
                            ),
                        ),
                        width,
                    )
                )
        if step["partial_output_status"] == "invalid":
            lines.append(
                _fit(
                    "  Failed-turn output evidence invalid | provider failure retained%s"
                    % (
                        " | token budget exhausted"
                        if step["runtime_budget_exhausted"]
                        else ""
                    ),
                    width,
                )
            )
        elif step["partial_output_preserved"]:
            lines.append(
                _fit(
                    "  Failed-turn output preserved: %d bytes from %s | sha256 %s | provider failure retained%s"
                    % (
                        step["partial_output_bytes"],
                        step["partial_output_source"],
                        step["partial_output_sha256"][:12],
                        " | token budget exhausted"
                        if step["runtime_budget_exhausted"]
                        else "",
                    ),
                    width,
                )
            )
    return "\n".join(lines) + "\n"


def _progress_summary(value: Dict) -> Dict:
    if value.get("status") == "invalid":
        return {"status": "invalid", "error": _text(value.get("error") or "unknown", 100)}
    result = {
        "status": _enum(value.get("status"), _PROGRESS_STATUSES, "unknown"),
        "last_event": _enum(value.get("last_event"), _PROGRESS_LAST_EVENTS, "unknown"),
        "event_count": _non_negative_int(value.get("event_count")),
        "item_started_count": _non_negative_int(value.get("item_started_count")),
        "item_completed_count": _non_negative_int(value.get("item_completed_count")),
        "failed_item_count": _non_negative_int(value.get("failed_item_count")),
        "collab_tool_call_count": _non_negative_int(
            value.get("collab_tool_call_count")
        ),
        "collab_tool_failed_count": _non_negative_int(
            value.get("collab_tool_failed_count")
        ),
        "native_agent_count": _non_negative_int(value.get("native_agent_count")),
        "native_agent_active_count": _non_negative_int(
            value.get("native_agent_active_count")
        ),
        "native_agent_error_count": _non_negative_int(
            value.get("native_agent_error_count")
        ),
        "native_agent_observation_truncated": (
            value.get("native_agent_observation_truncated") is True
        ),
        "native_agent_usage_attributed": (
            value.get("native_agent_usage_attributed") is True
        ),
        "native_agents_enabled_by_observer": (
            value.get("native_agents_enabled_by_observer") is True
        ),
        "native_agents_enabled_by_runner": (
            value.get("native_agents_enabled_by_runner") is True
        ),
        "native_agent_max_threads": _nullable_non_negative_int(
            value.get("native_agent_max_threads")
        ),
        "native_agent_max_depth": _nullable_non_negative_int(
            value.get("native_agent_max_depth")
        ),
        "native_agent_usage_status": _enum(
            value.get("native_agent_usage_status"),
            _NATIVE_USAGE_STATUSES,
            (
                "unavailable"
                if value.get("native_agents_enabled_by_runner") is True
                else "not-requested"
            ),
        ),
        "native_agent_usage_session_count": _non_negative_int(
            value.get("native_agent_usage_session_count")
        ),
        "native_agent_usage_child_count": _non_negative_int(
            value.get("native_agent_usage_child_count")
        ),
        "native_agent_usage_input_tokens": _nullable_non_negative_int(
            value.get("native_agent_usage_input_tokens")
        ),
        "native_agent_usage_cached_input_tokens": _nullable_non_negative_int(
            value.get("native_agent_usage_cached_input_tokens")
        ),
        "native_agent_usage_output_tokens": _nullable_non_negative_int(
            value.get("native_agent_usage_output_tokens")
        ),
        "native_agent_usage_total_tokens": _nullable_non_negative_int(
            value.get("native_agent_usage_total_tokens")
        ),
        "native_agent_usage_rollout_tokens": _nullable_non_negative_int(
            value.get("native_agent_usage_rollout_tokens")
        ),
        "total_tokens": _nullable_non_negative_int(value.get("total_tokens")),
    }
    return result


def _packet_progress_summary(values) -> Dict:
    values = values if isinstance(values, list) else []
    invalid = any(isinstance(value, dict) and value.get("status") == "invalid" for value in values)
    valid = [value for value in values if isinstance(value, dict) and value.get("status") != "invalid"]
    return {
        "count": _non_negative_int(len(values)),
        "active": sum(value.get("status") == "active" for value in valid),
        "completed": sum(value.get("status") == "completed" for value in valid),
        "failed": sum(value.get("status") not in {"active", "completed"} for value in valid),
        "invalid": invalid,
        "event_count": _bounded_sum(_non_negative_int(value.get("event_count")) for value in valid),
        "item_started_count": _bounded_sum(
            _non_negative_int(value.get("item_started_count")) for value in valid
        ),
        "item_completed_count": _bounded_sum(
            _non_negative_int(value.get("item_completed_count")) for value in valid
        ),
        "collab_tool_call_count": _bounded_sum(
            _non_negative_int(value.get("collab_tool_call_count")) for value in valid
        ),
        "collab_tool_failed_count": _bounded_sum(
            _non_negative_int(value.get("collab_tool_failed_count")) for value in valid
        ),
        "native_agent_count": _bounded_sum(
            _non_negative_int(value.get("native_agent_count")) for value in valid
        ),
        "native_agent_active_count": _bounded_sum(
            _non_negative_int(value.get("native_agent_active_count")) for value in valid
        ),
        "native_agent_error_count": _bounded_sum(
            _non_negative_int(value.get("native_agent_error_count")) for value in valid
        ),
        "native_agent_observation_truncated": any(
            value.get("native_agent_observation_truncated") is True for value in valid
        ),
    }


def _progress_text(progress: Dict) -> str:
    if progress.get("status") == "invalid":
        return "progress invalid (%s)" % progress.get("error", "unknown")
    tokens = (
        " | %d gross tokens" % progress["total_tokens"]
        if progress.get("total_tokens") is not None
        else ""
    )
    native = ""
    if progress["native_agents_enabled_by_runner"]:
        usage = "usage %s" % progress["native_agent_usage_status"]
        if progress["native_agent_usage_status"] == "complete":
            usage = "%d weighted/%d gross tokens across %d sessions/%d children" % (
                progress["native_agent_usage_rollout_tokens"],
                progress["native_agent_usage_total_tokens"],
                progress["native_agent_usage_session_count"],
                progress["native_agent_usage_child_count"],
            )
        native = (
            " | native enabled max %d depth %d | %d observed/%d active/%d errors | "
            "%d collab/%d failed | %s"
            % (
                progress["native_agent_max_threads"],
                progress["native_agent_max_depth"],
                progress["native_agent_count"],
                progress["native_agent_active_count"],
                progress["native_agent_error_count"],
                progress["collab_tool_call_count"],
                progress["collab_tool_failed_count"],
                usage,
            )
        )
    elif progress["native_agent_count"] or progress["collab_tool_call_count"]:
        native = (
            " | native %d/%d active/%d errors | %d collab/%d failed | usage unattributed"
            % (
                progress["native_agent_count"],
                progress["native_agent_active_count"],
                progress["native_agent_error_count"],
                progress["collab_tool_call_count"],
                progress["collab_tool_failed_count"],
            )
        )
        if progress["native_agent_observation_truncated"]:
            native += " | observation truncated"
    return "%s | %d events | %d/%d items | %d failed | last %s%s%s" % (
        progress["status"],
        progress["event_count"],
        progress["item_completed_count"],
        progress["item_started_count"],
        progress["failed_item_count"],
        progress["last_event"],
        native,
        tokens,
    )


def _duration(value) -> str:
    if value is None:
        return ""
    seconds = max(0, int(value)) / 1000.0
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm%02ds" % (int(seconds) // 60, int(seconds) % 60)


def _fit(value: str, width: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _text(value, limit: int) -> str:
    return " ".join(redact_terminal_text(str(value or "")).split())[:limit]


def _non_negative_int(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return min(value, RUN_CONSOLE_MAX_COUNT)
    return 0


def _nullable_non_negative_int(value):
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return min(value, RUN_CONSOLE_MAX_COUNT)
    return None


def _sha256_or_none(value):
    return value if _is_sha256(value) else None


def _is_sha256(value) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _duration_value(value):
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= RUN_CONSOLE_MAX_DURATION_MS
    ):
        return value
    return None


def _bounded_sum(values) -> int:
    total = 0
    for value in values:
        total = min(RUN_CONSOLE_MAX_COUNT, total + value)
    return total


def _enum(value, allowed, default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _validate_safe_text(value, limit: int, name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value):
        raise ValidationError("%s is invalid" % name)
    if value != " ".join(value.split()) or redact_terminal_text(value) != value:
        raise ValidationError("%s is not terminal-safe" % name)


def _validate_count(value, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= RUN_CONSOLE_MAX_COUNT
    ):
        raise ValidationError("%s is invalid" % name)


def _validate_nullable_count(value, name: str) -> None:
    if value is not None:
        _validate_count(value, name)


def _validate_progress(progress) -> None:
    if progress is None:
        return
    if not isinstance(progress, dict):
        raise ValidationError("run console Codex progress is invalid")
    if progress.get("status") == "invalid":
        if set(progress) != _INVALID_PROGRESS_FIELDS:
            raise ValidationError("run console invalid Codex progress fields are invalid")
        _validate_safe_text(progress.get("error"), 100, "run console Codex progress error")
        return
    if set(progress) != _VALID_PROGRESS_FIELDS or progress.get("status") not in _PROGRESS_STATUSES - {"invalid"}:
        raise ValidationError("run console Codex progress fields are invalid")
    if progress.get("last_event") not in _PROGRESS_LAST_EVENTS:
        raise ValidationError("run console Codex progress event is invalid")
    for field in (
        "event_count",
        "item_started_count",
        "item_completed_count",
        "failed_item_count",
        "collab_tool_call_count",
        "collab_tool_failed_count",
        "native_agent_count",
        "native_agent_active_count",
        "native_agent_error_count",
    ):
        _validate_count(progress.get(field), "run console Codex progress %s" % field)
    if progress["failed_item_count"] > progress["item_completed_count"]:
        raise ValidationError("run console Codex progress failed count is invalid")
    if progress["collab_tool_failed_count"] > progress["collab_tool_call_count"]:
        raise ValidationError("run console Codex collaboration failure count is invalid")
    if progress["native_agent_active_count"] > progress["native_agent_count"]:
        raise ValidationError("run console active native agent count is invalid")
    if progress["native_agent_error_count"] > progress["native_agent_count"]:
        raise ValidationError("run console native agent error count is invalid")
    if not isinstance(progress.get("native_agent_observation_truncated"), bool):
        raise ValidationError("run console native agent truncation marker is invalid")
    if not isinstance(progress.get("native_agent_usage_attributed"), bool):
        raise ValidationError("run console native agent usage attribution is invalid")
    if progress.get("native_agents_enabled_by_observer") is not False:
        raise ValidationError("run console native agent enablement marker is invalid")
    if not isinstance(progress.get("native_agents_enabled_by_runner"), bool):
        raise ValidationError("run console runner native agent enablement marker is invalid")
    _validate_nullable_count(
        progress.get("native_agent_max_threads"),
        "run console native agent max threads",
    )
    _validate_nullable_count(
        progress.get("native_agent_max_depth"),
        "run console native agent max depth",
    )
    if progress["native_agents_enabled_by_runner"]:
        if (
            progress["native_agent_max_threads"] is None
            or not MIN_CODEX_NATIVE_AGENT_THREADS
            <= progress["native_agent_max_threads"]
            <= MAX_CODEX_NATIVE_AGENT_THREADS
            or progress["native_agent_max_depth"] != 1
        ):
            raise ValidationError("run console native agent limits are invalid")
    elif (
        progress["native_agent_max_threads"] is not None
        or progress["native_agent_max_depth"] is not None
    ):
        raise ValidationError("run console disabled native agent limits must be null")
    usage_status = progress.get("native_agent_usage_status")
    if usage_status not in _NATIVE_USAGE_STATUSES:
        raise ValidationError("run console native usage status is invalid")
    usage = {
        "status": usage_status if usage_status != "pending" else "unavailable",
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
    if progress["native_agents_enabled_by_runner"]:
        if usage_status == "not-requested":
            raise ValidationError("enabled run console native usage is inconsistent")
    elif usage_status != "not-requested":
        raise ValidationError("disabled run console native usage is inconsistent")
    if progress["native_agent_usage_attributed"] != (usage_status == "complete"):
        raise ValidationError("run console native usage attribution is inconsistent")
    _validate_nullable_count(progress.get("total_tokens"), "run console Codex progress tokens")


def _validate_packet_progress(progress) -> None:
    if not isinstance(progress, dict) or set(progress) != _PACKET_PROGRESS_FIELDS:
        raise ValidationError("run console packet progress is invalid")
    for field in _PACKET_PROGRESS_FIELDS - {
        "invalid",
        "native_agent_observation_truncated",
    }:
        _validate_count(progress.get(field), "run console packet progress %s" % field)
    if not isinstance(progress.get("invalid"), bool):
        raise ValidationError("run console packet progress invalid marker is invalid")
    if not isinstance(progress.get("native_agent_observation_truncated"), bool):
        raise ValidationError("run console packet native agent truncation marker is invalid")
    if progress["active"] + progress["completed"] + progress["failed"] > progress["count"]:
        raise ValidationError("run console packet progress status counts are invalid")
    if progress["collab_tool_failed_count"] > progress["collab_tool_call_count"]:
        raise ValidationError("run console packet collaboration failure count is invalid")
    if progress["native_agent_active_count"] > progress["native_agent_count"]:
        raise ValidationError("run console packet active native agent count is invalid")
    if progress["native_agent_error_count"] > progress["native_agent_count"]:
        raise ValidationError("run console packet native agent error count is invalid")


def _validate_step(step) -> None:
    if not isinstance(step, dict) or set(step) != _STEP_FIELDS:
        raise ValidationError("run console step is invalid")
    for field, limit in (("id", 128), ("status", 64), ("phase", 128)):
        _validate_safe_text(step.get(field), limit, "run console step %s" % field)
    _validate_safe_text(step.get("kind"), 128, "run console step kind", allow_empty=True)
    duration = step.get("duration_ms")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= RUN_CONSOLE_MAX_DURATION_MS
    ):
        raise ValidationError("run console step duration is invalid")
    if not isinstance(step.get("runtime_budget_exhausted"), bool):
        raise ValidationError("run console step budget marker is invalid")
    if not isinstance(step.get("partial_output_preserved"), bool):
        raise ValidationError("run console step partial-output marker is invalid")
    if step.get("partial_output_status") not in _PARTIAL_OUTPUT_STATUSES:
        raise ValidationError("run console step partial-output status is invalid")
    partial_values = (
        step.get("partial_output_source"),
        step.get("partial_output_sha256"),
        step.get("partial_output_bytes"),
    )
    if step["partial_output_status"] == "valid":
        if step["partial_output_preserved"] is not True:
            raise ValidationError("run console step valid partial-output marker is inconsistent")
        if step["partial_output_source"] not in _PARTIAL_OUTPUT_SOURCES:
            raise ValidationError("run console step partial-output source is invalid")
        if not _is_sha256(step["partial_output_sha256"]):
            raise ValidationError("run console step partial-output hash is invalid")
        if (
            isinstance(step["partial_output_bytes"], bool)
            or not isinstance(step["partial_output_bytes"], int)
            or not 1 <= step["partial_output_bytes"] <= RUN_CONSOLE_MAX_COUNT
        ):
            raise ValidationError("run console step partial-output size is invalid")
    elif step["partial_output_preserved"] or partial_values != (None, None, None):
        raise ValidationError("run console step unavailable partial-output evidence must be null")
    _validate_progress(step.get("codex_progress"))
    _validate_packet_progress(step.get("codex_packet_progress"))
