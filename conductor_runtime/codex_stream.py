import json
from typing import Dict

from .codex_config import MAX_CODEX_TOKENS
from .errors import ValidationError
from .redaction import redact_text


MAX_CODEX_STREAM_EVENTS = 20000
MAX_CODEX_STREAM_LINE_BYTES = 1024 * 1024


def parse_codex_stream(text: str) -> Dict:
    parsed = analyze_codex_stream(text)
    if parsed["parse_error"] is not None:
        raise ValidationError(parsed["parse_error"])
    return parsed


def analyze_codex_stream(text: str) -> Dict:
    events = 0
    thread_started = 0
    turns_started = 0
    turns_completed = 0
    turns_failed = 0
    provider_error_events = 0
    runtime_budget_exhausted = False
    agent_texts = []
    terminal_usage = None
    token_usage_source = "none"
    parse_error = None
    for line_number, raw_line in enumerate(str(text or "").splitlines(), start=1):
        if not raw_line.strip():
            continue
        if len(raw_line.encode("utf-8")) > MAX_CODEX_STREAM_LINE_BYTES:
            parse_error = "Codex stream line %d exceeds the supported size" % line_number
            break
        events += 1
        if events > MAX_CODEX_STREAM_EVENTS:
            parse_error = "Codex stream exceeds the supported event count"
            break
        try:
            event = json.loads(
                raw_line,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            detail = redact_text(str(exc))[:300] or exc.__class__.__name__
            parse_error = "Codex stream line %d is not strict JSON: %s" % (
                line_number,
                detail,
            )
            break
        if not isinstance(event, dict):
            parse_error = "Codex stream line %d must contain an object" % line_number
            break
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            parse_error = "Codex stream line %d is missing an event type" % line_number
            break
        if event_type == "thread.started":
            thread_started += 1
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 200:
                parse_error = "Codex thread.started event has an invalid thread id"
                break
        elif event_type == "turn.started":
            turns_started += 1
        elif event_type == "turn.completed":
            turns_completed += 1
            if turns_completed > 1:
                parse_error = "Codex stream contains multiple terminal turn.completed events"
                break
            try:
                terminal_usage = _codex_usage(event.get("usage"))
                token_usage_source = "turn.completed"
            except ValidationError as exc:
                parse_error = str(exc)
                break
        elif event_type == "turn.failed":
            turns_failed += 1
            provider_error_events += 1
            runtime_budget_exhausted = (
                runtime_budget_exhausted or _is_rollout_budget_error(event)
            )
            if "usage" in event:
                try:
                    terminal_usage = _codex_usage(event.get("usage"))
                    token_usage_source = "turn.failed"
                except ValidationError as exc:
                    parse_error = str(exc)
                    break
        elif event_type == "error":
            provider_error_events += 1
            runtime_budget_exhausted = (
                runtime_budget_exhausted or _is_rollout_budget_error(event)
            )
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                parse_error = "Codex item.completed event must contain an item object"
                break
            if item.get("type") == "agent_message":
                message = item.get("text")
                if not isinstance(message, str):
                    parse_error = "Codex agent_message item must contain text"
                    break
                agent_texts.append(message)
    if parse_error is None and events == 0:
        parse_error = "Codex stream contains no JSON events"
    if parse_error is None and thread_started != 1:
        parse_error = "Codex stream must contain exactly one thread.started event"
    if parse_error is None and turns_started > 1:
        parse_error = "Codex stream contains multiple turn.started events"
    if parse_error is None and turns_completed + turns_failed != 1:
        parse_error = "Codex stream must contain exactly one terminal turn event"
    if parse_error is None and turns_failed == 1 and runtime_budget_exhausted is False:
        terminal_status = "failed"
    elif turns_failed == 1 and runtime_budget_exhausted:
        terminal_status = "budget-exhausted"
    elif turns_completed == 1:
        terminal_status = "completed"
    else:
        terminal_status = "missing"
    usage = terminal_usage or {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
    }
    output_text = "\n\n".join(value for value in agent_texts if value).strip()
    return {
        "stream_events": events,
        "thread_started": thread_started,
        "turns": turns_completed + turns_failed,
        "agent_messages": len(agent_texts),
        "provider_error_events": provider_error_events,
        "terminal_event_present": turns_completed + turns_failed == 1,
        "terminal_status": terminal_status,
        "runtime_budget_exhausted": runtime_budget_exhausted,
        "token_usage_source": token_usage_source,
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_output_tokens": usage["reasoning_output_tokens"],
        "total_tokens": usage["total_tokens"],
        "output_text": output_text,
        "output_source": "agent-messages" if output_text else "none",
        "parse_error": redact_text(parse_error) if parse_error else None,
    }


def _codex_usage(value) -> Dict:
    if not isinstance(value, dict):
        raise ValidationError("Codex terminal usage must be an object")
    allowed = {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(
            "Codex terminal usage contains unsupported fields: %s" % ", ".join(unknown)
        )
    cleaned = {
        field: _bounded_int(
            value.get(field),
            "Codex terminal usage %s" % field,
            0,
            MAX_CODEX_TOKENS,
        )
        for field in sorted(allowed)
    }
    if cleaned["cached_input_tokens"] > cleaned["input_tokens"]:
        raise ValidationError("Codex cached input tokens exceed input tokens")
    if cleaned["reasoning_output_tokens"] > cleaned["output_tokens"]:
        raise ValidationError("Codex reasoning output tokens exceed output tokens")
    cleaned["total_tokens"] = cleaned["input_tokens"] + cleaned["output_tokens"]
    if cleaned["total_tokens"] > MAX_CODEX_TOKENS:
        raise ValidationError("Codex terminal total tokens exceed the supported limit")
    return cleaned


def _is_rollout_budget_error(event: Dict) -> bool:
    values = []
    message = event.get("message") if isinstance(event, dict) else None
    if isinstance(message, str):
        values.append(message)
    error = event.get("error") if isinstance(event, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        values.append(error["message"])
    return any("rollout token budget exhausted" in value.lower() for value in values)


def _bounded_int(value, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError(
            "%s must be an integer from %d to %d" % (label, minimum, maximum)
        )
    return value


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key %s" % key)
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant %s" % value)
