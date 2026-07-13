import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import ValidationError
from .redaction import redact_text
from .security import read_regular_text_file_no_follow


MAX_SESSION_TRACE_JSON_BYTES = 1024 * 1024
MAX_SESSION_TRACE_EVENTS = 500
MAX_SESSION_TRACE_DEPTH = 16
MAX_TRACE_TEXT_CHARS = 300
MAX_TRACE_DURATION_MS = 30 * 24 * 60 * 60 * 1000
TRACE_LIST_FIELDS = ("events", "trace_events", "session_trace_events", "session_trace")
STRING_FIELDS = ("provider", "event", "status", "session_id", "agent_id", "step_id", "model", "role", "tool", "detail")
PROVIDER_VALUES = {"generic", "openai", "anthropic", "codex", "mixed"}
RAW_TRANSCRIPT_FIELDS = {"arguments", "content", "description", "input", "message", "messages", "output", "text"}
USAGE_INPUT_FIELDS = ("input_tokens", "prompt_tokens", "input_token_count", "prompt_token_count")
USAGE_OUTPUT_FIELDS = ("output_tokens", "completion_tokens", "output_token_count", "completion_token_count")
USAGE_TOTAL_FIELDS = ("total_tokens", "total_token_count")
USAGE_COST_FIELDS = ("cost_usd", "total_cost_usd")
USAGE_CACHE_INPUT_FIELDS = ("cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass(frozen=True)
class ImportedSessionTrace:
    events: List[Dict]
    provider: str = "generic"


@dataclass(frozen=True)
class ImportedSessionUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    provider: str = "generic"

    def as_record_kwargs(self) -> Dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


def load_session_trace_import(path: Path, provider: str = "auto", step_id: str = "") -> ImportedSessionTrace:
    text = read_regular_text_file_no_follow(path, "session trace artifact", MAX_SESSION_TRACE_JSON_BYTES)
    data = _load_json_or_jsonl(text)
    return parse_session_trace_import(data, provider=provider, step_id=step_id)


def _load_json_or_jsonl(text: str):
    try:
        return json.loads(
            text,
            parse_float=_finite_json_float,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        return _load_jsonl(text, exc)
    except RecursionError:
        raise ValidationError("session trace artifact nesting is too deep")


def _load_jsonl(text: str, json_error: json.JSONDecodeError) -> List[Dict]:
    events = []
    saw_nonblank_line = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        saw_nonblank_line = True
        try:
            raw = json.loads(
                line,
                parse_float=_finite_json_float,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except json.JSONDecodeError as exc:
            raise ValidationError("session trace artifact is not valid JSON or JSONL at line %d: %s" % (line_number, exc))
        except RecursionError:
            raise ValidationError("session trace artifact nesting is too deep")
        if not isinstance(raw, dict):
            raise ValidationError("session trace JSONL line %d must contain a JSON object" % line_number)
        if len(events) >= MAX_SESSION_TRACE_EVENTS:
            raise ValidationError("session trace artifact contains more than %d events" % MAX_SESSION_TRACE_EVENTS)
        events.append(raw)
    if not saw_nonblank_line:
        raise ValidationError("session trace artifact is not valid JSON: %s" % json_error)
    return events


def parse_session_trace_import(data, provider: str = "auto", step_id: str = "") -> ImportedSessionTrace:
    _check_depth(data)
    normalized_provider = _normalize_provider(provider)
    raw_events = _raw_events(data)
    if not raw_events:
        raise ValidationError("session trace artifact does not contain supported trace events")
    if len(raw_events) > MAX_SESSION_TRACE_EVENTS:
        raise ValidationError("session trace artifact contains more than %d events" % MAX_SESSION_TRACE_EVENTS)

    events = []
    providers = []
    for raw, context in raw_events:
        adapted = _adapt_event_mapping(raw, context)
        event = _event_from_mapping(adapted, context, normalized_provider, step_id=step_id)
        events.append(event)
        providers.append(event.get("provider") or "generic")
    provider_name = providers[0] if providers and all(provider == providers[0] for provider in providers) else "mixed"
    return ImportedSessionTrace(events=events, provider=provider_name)


def aggregate_session_trace_usage(events: List[Dict], provider: str = "generic") -> ImportedSessionUsage:
    input_total = 0
    output_total = 0
    total_total = 0
    cost_total = 0.0
    has_input = False
    has_output = False
    has_total = False
    has_cost = False
    for event in events:
        if not isinstance(event, dict):
            continue
        input_tokens = _optional_nonnegative_int(event.get("input_tokens"), "input_tokens", 10**12)
        output_tokens = _optional_nonnegative_int(event.get("output_tokens"), "output_tokens", 10**12)
        total_tokens = _optional_nonnegative_int(event.get("total_tokens"), "total_tokens", 10**12)
        cost_usd = _optional_nonnegative_number(event.get("cost_usd"), "cost_usd")
        if input_tokens is not None:
            input_total += input_tokens
            _validate_trace_int_bound(input_total, "input_tokens", 10**12)
            has_input = True
        if output_tokens is not None:
            output_total += output_tokens
            _validate_trace_int_bound(output_total, "output_tokens", 10**12)
            has_output = True
        if total_tokens is not None:
            total_total += total_tokens
            _validate_trace_int_bound(total_total, "total_tokens", 10**12)
            has_total = True
        if cost_usd is not None:
            cost_total += cost_usd
            if not math.isfinite(cost_total):
                raise ValidationError("session trace artifact cost_usd must be a non-negative finite number")
            has_cost = True
    if not any([has_input, has_output, has_total, has_cost]):
        raise ValidationError("session trace artifact does not contain measured usage fields")
    record_input = input_total if has_input else None
    record_output = output_total if has_output else None
    record_total = total_total if has_total else None
    if has_total and (has_input or has_output):
        if not (has_input and has_output) or total_total != input_total + output_total:
            raise ValidationError("session trace artifact contains ambiguous aggregate usage fields")
    elif not has_total:
        record_total = None
    return ImportedSessionUsage(
        input_tokens=record_input,
        output_tokens=record_output,
        total_tokens=record_total,
        cost_usd=round(cost_total, 12) if has_cost else None,
        provider=_normalize_provider(provider) if provider else "generic",
    )


def _raw_events(data) -> List[Tuple[Dict, Dict]]:
    events: List[Tuple[Dict, Dict]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                raise ValidationError("session trace events must be objects")
            _require_supported_event_mapping(item)
            events.append((item, {}))
        return events
    if not isinstance(data, dict):
        raise ValidationError("session trace artifact must be a JSON object or array")

    root_context = _context(data)
    provider_output_events = _events_from_provider_output_container(data, root_context)
    if provider_output_events:
        return provider_output_events
    sessions = data.get("sessions")
    if isinstance(sessions, list):
        for session in sessions:
            if not isinstance(session, dict):
                raise ValidationError("session trace sessions must contain objects")
            session_context = _merge_context(root_context, _context(session))
            for raw_event in _events_from_container(session):
                events.append((raw_event, session_context))
        return events
    for field in TRACE_LIST_FIELDS:
        value = data.get(field)
        if isinstance(value, list):
            for raw_event in value:
                if not isinstance(raw_event, dict):
                    raise ValidationError("session trace events must be objects")
                _require_supported_event_mapping(raw_event)
                events.append((raw_event, root_context))
            return events
    if _looks_like_event(data):
        return [(data, root_context)]
    raise ValidationError("session trace artifact does not contain supported trace events")


def _events_from_container(container: Dict) -> List[Dict]:
    provider_output_events = _events_from_provider_output_container(container, _context(container))
    if provider_output_events:
        return [_raw_with_context(raw_event, event_context) for raw_event, event_context in provider_output_events]
    for field in TRACE_LIST_FIELDS:
        value = container.get(field)
        if isinstance(value, list):
            events = []
            for raw_event in value:
                if not isinstance(raw_event, dict):
                    raise ValidationError("session trace events must be objects")
                _require_supported_event_mapping(raw_event)
                events.append(raw_event)
            return events
    if _looks_like_event(container):
        return [container]
    return []


def _raw_with_context(raw: Dict, context: Dict) -> Dict:
    merged = dict(raw)
    for field in ("provider", "session_id", "agent_id", "step_id", "model", "role", "tool"):
        if field not in merged and context.get(field):
            merged[field] = context[field]
    return merged


def _require_supported_event_mapping(raw: Dict) -> None:
    if _looks_like_event(raw):
        return
    adapted = _adapt_event_mapping(raw, {})
    if _looks_like_event(adapted) or _has_usage_fields(adapted):
        return
    raise ValidationError("session trace artifact does not contain supported trace events")


def _events_from_provider_output_container(container: Dict, context: Dict) -> List[Tuple[Dict, Dict]]:
    output = container.get("output")
    if not isinstance(output, list) or not _looks_like_provider_output_container(container):
        return []
    events: List[Tuple[Dict, Dict]] = []
    root = _adapt_event_mapping(container, context)
    if _looks_like_event(root) or _has_usage_mapping(container):
        events.append((root, context))
    provider_context = dict(context)
    if not provider_context.get("provider") and _looks_like_openai_response(container):
        provider_context["provider"] = "openai"
    for raw_event in output:
        if not isinstance(raw_event, dict):
            raise ValidationError("session trace provider output entries must be objects")
        _require_supported_event_mapping(raw_event)
        events.append((raw_event, provider_context))
    return events


def _looks_like_provider_output_container(raw: Dict) -> bool:
    if not isinstance(raw.get("output"), list):
        return False
    if _looks_like_openai_response(raw):
        return True
    return bool(isinstance(raw.get("usage"), dict) and (_short_text(raw.get("model")) or _short_text(raw.get("status"))))


def _looks_like_openai_response(raw: Dict) -> bool:
    if _short_text(raw.get("object")).lower() == "response":
        return True
    if _short_text(raw.get("type")).lower() == "response":
        return True
    identifier = _short_text(raw.get("id")).lower()
    return bool(identifier.startswith("resp_") and isinstance(raw.get("output"), list))


def _adapt_event_mapping(raw: Dict, context: Dict) -> Dict:
    adapted = {key: value for key, value in raw.items() if key not in RAW_TRANSCRIPT_FIELDS}
    if _looks_like_openai_response(raw) and "provider" not in adapted:
        adapted["provider"] = "openai"
    if _looks_like_openai_response(raw) and "event" not in adapted and "type" not in adapted:
        adapted["event"] = "response"
    message = raw.get("message")
    if isinstance(message, dict):
        _merge_message_metadata(adapted, message)
    _merge_usage_mapping(adapted, raw.get("usage"))
    if isinstance(message, dict):
        _merge_usage_mapping(adapted, message.get("usage"))
    return adapted


def _merge_message_metadata(target: Dict, message: Dict) -> None:
    for field in ("provider", "status", "session_id", "agent_id", "step_id", "model", "role", "tool"):
        if field not in target and field in message:
            target[field] = message[field]
    if "event" not in target and "type" not in target:
        content_event, content_tool = _message_content_metadata(message)
        target["event"] = content_event or message.get("type") or "message"
    else:
        _content_event, content_tool = _message_content_metadata(message)
    if content_tool and "tool" not in target:
        target["tool"] = content_tool


def _message_content_metadata(message: Dict) -> Tuple[str, str]:
    content = message.get("content")
    if not isinstance(content, list):
        return "", ""
    fallback_event = ""
    for item in content[:20]:
        if not isinstance(item, dict):
            continue
        item_type = _short_text(item.get("type"))
        if item_type and not fallback_event:
            fallback_event = item_type
        item_name = _short_text(item.get("name"))
        if item_type and item_type != "text":
            return item_type, item_name
    return fallback_event, ""


def _has_usage_mapping(raw: Dict) -> bool:
    return isinstance(raw.get("usage"), dict)


def _has_usage_fields(raw: Dict) -> bool:
    return any(field in raw for field in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"))


def _merge_usage_mapping(target: Dict, usage) -> None:
    if not isinstance(usage, dict):
        return
    input_tokens = _consistent_usage_int_alias(usage, USAGE_INPUT_FIELDS, "input_tokens")
    output_tokens = _consistent_usage_int_alias(usage, USAGE_OUTPUT_FIELDS, "output_tokens")
    total_tokens = _consistent_usage_int_alias(usage, USAGE_TOTAL_FIELDS, "total_tokens")
    cache_input = _sum_usage_ints(usage, USAGE_CACHE_INPUT_FIELDS)
    if cache_input:
        input_tokens = (input_tokens or 0) + cache_input
        _validate_trace_int_bound(input_tokens, "input_tokens", 10**12)
    _set_usage_field(target, "input_tokens", input_tokens)
    _set_usage_field(target, "output_tokens", output_tokens)
    _set_usage_field(target, "total_tokens", total_tokens)
    cost = _consistent_usage_number_alias(usage, USAGE_COST_FIELDS, "cost_usd")
    if cost is not None and "cost_usd" not in target:
        target["cost_usd"] = cost


def _set_usage_field(target: Dict, field: str, value: Optional[int]) -> None:
    if value is not None and field not in target:
        target[field] = value


def _consistent_usage_int_alias(raw: Dict, fields: Tuple[str, ...], label: str) -> Optional[int]:
    values = []
    for field in fields:
        if field in raw:
            values.append(_optional_nonnegative_int(raw[field], field, 10**12))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValidationError("session trace artifact has conflicting %s aliases" % label)
    return values[0]


def _sum_usage_ints(raw: Dict, fields: Tuple[str, ...]) -> int:
    total = 0
    for field in fields:
        if field in raw:
            value = _optional_nonnegative_int(raw[field], field, 10**12)
            if value is not None:
                total += value
            _validate_trace_int_bound(total, field, 10**12)
    return total


def _consistent_usage_number_alias(raw: Dict, fields: Tuple[str, ...], label: str):
    values = []
    for field in fields:
        if field in raw:
            values.append(_optional_nonnegative_number(raw[field], field))
    if not values:
        return None
    if any(float(value) != float(values[0]) for value in values[1:]):
        raise ValidationError("session trace artifact has conflicting %s aliases" % label)
    return values[0]


def _event_from_mapping(raw: Dict, context: Dict, provider: str, step_id: str = "") -> Dict:
    event_provider = _short_text(raw.get("provider") or context.get("provider"))
    if provider != "auto":
        event_provider = provider
    elif not event_provider:
        event_provider = _detect_provider(raw, context)
    event_name = _first_text(raw, ("event", "type", "name", "phase")) or "event"
    entry = {
        "provider": _normalize_observed_provider(event_provider),
        "event": event_name,
    }
    for field in ["status", "session_id", "agent_id", "model", "role"]:
        value = _short_text(raw.get(field) or context.get(field))
        if value:
            entry[field] = value
    tool = _tool_name(raw, context)
    if tool:
        entry["tool"] = tool
    effective_step_id = step_id or raw.get("step_id") or context.get("step_id")
    if effective_step_id:
        entry["step_id"] = _short_text(effective_step_id)
    detail = _first_text(raw, ("detail", "summary"))
    if detail:
        entry["detail"] = detail
    for source, target in [
        ("started_at_utc", "started_at_utc"),
        ("finished_at_utc", "finished_at_utc"),
        ("timestamp", "recorded_at_utc"),
        ("recorded_at_utc", "recorded_at_utc"),
        ("created_at", "recorded_at_utc"),
    ]:
        value = _short_text(raw.get(source))
        if value and target not in entry:
            entry[target] = value
    duration_ms = _optional_nonnegative_int(raw.get("duration_ms"), "duration_ms", MAX_TRACE_DURATION_MS)
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    input_tokens = _optional_nonnegative_int(raw.get("input_tokens"), "input_tokens", 10**12)
    output_tokens = _optional_nonnegative_int(raw.get("output_tokens"), "output_tokens", 10**12)
    total_tokens = _optional_nonnegative_int(raw.get("total_tokens"), "total_tokens", 10**12)
    if input_tokens is not None:
        entry["input_tokens"] = input_tokens
    if output_tokens is not None:
        entry["output_tokens"] = output_tokens
    if input_tokens is not None and output_tokens is not None:
        derived_total = input_tokens + output_tokens
        if derived_total > 10**12:
            raise ValidationError("session trace artifact total_tokens must be a non-negative integer no greater than %s" % 10**12)
        if total_tokens is None:
            total_tokens = derived_total
        elif total_tokens != derived_total:
            raise ValidationError("session trace artifact total_tokens must equal input_tokens plus output_tokens")
    if total_tokens is not None:
        entry["total_tokens"] = total_tokens
    cost = _optional_nonnegative_number(raw.get("cost_usd"), "cost_usd")
    if cost is not None:
        entry["cost_usd"] = cost
    return entry


def _context(raw: Dict) -> Dict:
    return {field: _short_text(raw.get(field)) for field in ["provider", "session_id", "agent_id", "step_id", "model", "role", "tool"] if raw.get(field)}


def _merge_context(base: Dict, extra: Dict) -> Dict:
    merged = dict(base)
    merged.update({key: value for key, value in extra.items() if value})
    return merged


def _looks_like_event(raw: Dict) -> bool:
    for field in ("event", "type", "name", "phase", "status", "detail", "summary"):
        if _short_text(raw.get(field)):
            return True
    return _looks_like_message_event(raw.get("message"))


def _looks_like_message_event(message) -> bool:
    if not isinstance(message, dict):
        return False
    if any(field in message for field in ("provider", "status", "session_id", "agent_id", "step_id", "model", "role", "usage")):
        return True
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(item, dict) and item.get("type") for item in content[:20])


def _first_text(raw: Dict, fields: Tuple[str, ...]) -> str:
    for field in fields:
        value = _short_text(raw.get(field))
        if value:
            return value
    return ""


def _short_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = " ".join(str(value).split())
    if not text:
        return ""
    return redact_text(text)[:MAX_TRACE_TEXT_CHARS]


def _normalize_provider(provider: str) -> str:
    value = str(provider or "auto").strip().lower()
    if value not in {"auto", "generic", "openai", "anthropic", "codex", "claude", "mixed"}:
        raise ValidationError("unsupported session trace provider: %s" % provider)
    if value == "claude":
        return "anthropic"
    return value


def _normalize_observed_provider(provider: str) -> str:
    value = str(provider or "generic").strip().lower()
    if value == "claude":
        return "anthropic"
    if value in PROVIDER_VALUES:
        return value
    if "anthropic" in value or "claude" in value:
        return "anthropic"
    if "openai" in value:
        return "openai"
    if "codex" in value:
        return "codex"
    return "generic"


def _detect_provider(raw: Dict, context: Dict) -> str:
    provider_markers = []
    for source in (raw, context):
        for field in ("provider", "source", "vendor", "object", "id", "model"):
            value = _short_text(source.get(field))
            if value:
                provider_markers.append(value.lower())
    text = " ".join(provider_markers)
    if "anthropic" in text or "claude" in text:
        return "anthropic"
    if "openai" in text or "resp_" in text or "gpt-" in text or "gpt_" in text:
        return "openai"
    model = _short_text(raw.get("model") or context.get("model")).lower()
    if model.startswith(("o1", "o3", "o4")):
        return "openai"
    if "codex" in text:
        return "codex"
    return "generic"


def _tool_name(raw: Dict, context: Dict) -> str:
    value = _short_text(raw.get("tool") or context.get("tool"))
    if value:
        return value
    event_type = _short_text(raw.get("type") or raw.get("event") or raw.get("name")).lower()
    if any(marker in event_type for marker in ("tool", "function")):
        return _short_text(raw.get("name"))
    return ""


def _optional_nonnegative_int(value, label: str, maximum: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValidationError("session trace artifact %s must be a non-negative integer no greater than %s" % (label, maximum))
    return value


def _validate_trace_int_bound(value: int, label: str, maximum: int) -> None:
    if value > maximum:
        raise ValidationError("session trace artifact %s must be a non-negative integer no greater than %s" % (label, maximum))


def _optional_nonnegative_number(value, label: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValidationError("session trace artifact %s must be a non-negative finite number" % label)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValidationError("session trace artifact %s must be a non-negative finite number" % label)
    return float(value)


def _check_depth(value, depth: int = 0) -> None:
    if depth > MAX_SESSION_TRACE_DEPTH:
        raise ValidationError("session trace artifact nesting is too deep")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value[: MAX_SESSION_TRACE_EVENTS + 1]:
            _check_depth(item, depth + 1)


def _reject_json_constant(_value):
    raise ValidationError("session trace artifact must not contain non-standard JSON constants")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValidationError("session trace artifact must not contain non-finite JSON numbers")
    return parsed


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("session trace artifact contains duplicate JSON keys")
        result[key] = value
    return result
