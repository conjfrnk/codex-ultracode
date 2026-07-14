import json
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .errors import ValidationError


MAX_PROVIDER_EVENTS = 1000
MAX_PROVIDER_EVENT_LINE_BYTES = 256 * 1024
MAX_PROVIDER_TOKENS = 10**12

_TERMINAL_USAGE_EVENTS = {
    "result",
    "response.completed",
    "response_completed",
    "turn.completed",
    "turn_completed",
}


@dataclass(frozen=True)
class ProviderTelemetry:
    events: List[Dict]
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None

    @property
    def has_usage(self) -> bool:
        return any(
            value is not None
            for value in (self.input_tokens, self.output_tokens, self.total_tokens, self.cost_usd)
        )


def parse_provider_jsonl(text: str, provider: str) -> ProviderTelemetry:
    normalized_provider = _provider_name(provider)
    events = []
    terminal_usage = None
    for raw_line in str(text or "").splitlines():
        if not raw_line.strip():
            continue
        if len(raw_line.encode("utf-8")) > MAX_PROVIDER_EVENT_LINE_BYTES:
            continue
        try:
            raw = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(raw, dict):
            continue
        event = _normalized_event(raw, normalized_provider)
        if event is None:
            continue
        events.append(event)
        event_name = str(event.get("event") or "").lower()
        usage = _usage_from_mapping(raw)
        if usage is not None and event_name in _TERMINAL_USAGE_EVENTS:
            terminal_usage = usage
    events = events[-MAX_PROVIDER_EVENTS:]
    usage = terminal_usage or {}
    return ProviderTelemetry(
        events=events,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        cost_usd=usage.get("cost_usd"),
    )


def merge_provider_telemetry(values: Iterable[ProviderTelemetry]) -> ProviderTelemetry:
    events = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    cost_usd = 0.0
    has_input = False
    has_output = False
    has_total = False
    has_cost = False
    for telemetry in values:
        if not isinstance(telemetry, ProviderTelemetry):
            continue
        events.extend(telemetry.events)
        if telemetry.input_tokens is not None:
            input_tokens = _bounded_token_sum(input_tokens, telemetry.input_tokens, "input_tokens")
            has_input = True
        if telemetry.output_tokens is not None:
            output_tokens = _bounded_token_sum(output_tokens, telemetry.output_tokens, "output_tokens")
            has_output = True
        if telemetry.total_tokens is not None:
            total_tokens = _bounded_token_sum(total_tokens, telemetry.total_tokens, "total_tokens")
            has_total = True
        if telemetry.cost_usd is not None:
            cost_usd += telemetry.cost_usd
            if not math.isfinite(cost_usd):
                raise ValidationError("provider telemetry cost total must be finite")
            has_cost = True
    if has_input and has_output:
        derived_total = input_tokens + output_tokens
        if derived_total > MAX_PROVIDER_TOKENS:
            raise ValidationError("provider telemetry total_tokens exceeds the supported limit")
        if has_total and total_tokens != derived_total:
            raise ValidationError("provider telemetry total_tokens must equal input_tokens plus output_tokens")
        total_tokens = derived_total
        has_total = True
    return ProviderTelemetry(
        events=events[-MAX_PROVIDER_EVENTS:],
        input_tokens=input_tokens if has_input else None,
        output_tokens=output_tokens if has_output else None,
        total_tokens=total_tokens if has_total else None,
        cost_usd=round(cost_usd, 12) if has_cost else None,
    )


def _normalized_event(raw: Dict, provider: str) -> Optional[Dict]:
    event_name = _short_text(raw.get("type") or raw.get("event"))
    if not event_name:
        return None
    event = {"event": event_name, "provider": provider}
    status = _short_text(raw.get("status"))
    if status:
        event["status"] = status
    for source, destination in (
        ("thread_id", "session_id"),
        ("session_id", "session_id"),
        ("model", "model"),
    ):
        value = _short_text(raw.get(source))
        if value and destination not in event:
            event[destination] = value
    usage = _usage_from_mapping(raw)
    if usage:
        event.update(usage)
    return event


def _usage_from_mapping(raw: Dict) -> Optional[Dict]:
    candidates = []
    if isinstance(raw.get("usage"), dict):
        candidates.append(raw["usage"])
    message = raw.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        candidates.append(message["usage"])
    candidates.append(raw)
    for candidate in candidates:
        usage = _clean_usage(candidate, raw)
        if usage:
            return usage
    return None


def _clean_usage(raw: Dict, envelope: Dict) -> Dict:
    input_tokens = _first_int(raw, ("input_tokens", "prompt_tokens", "input_token_count"))
    output_tokens = _first_int(raw, ("output_tokens", "completion_tokens", "output_token_count"))
    total_tokens = _first_int(raw, ("total_tokens", "total_token_count"))
    cost_usd = _first_number(raw, ("cost_usd", "total_cost_usd"))
    if cost_usd is None:
        cost_usd = _first_number(envelope, ("cost_usd", "total_cost_usd"))
    if input_tokens is not None and output_tokens is not None:
        derived = input_tokens + output_tokens
        if derived > MAX_PROVIDER_TOKENS:
            raise ValidationError("provider telemetry total_tokens exceeds the supported limit")
        if total_tokens is not None and total_tokens != derived:
            raise ValidationError("provider telemetry total_tokens must equal input_tokens plus output_tokens")
        total_tokens = derived
    result = {}
    if input_tokens is not None:
        result["input_tokens"] = input_tokens
    if output_tokens is not None:
        result["output_tokens"] = output_tokens
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    if cost_usd is not None:
        result["cost_usd"] = cost_usd
    return result


def _first_int(raw: Dict, names) -> Optional[int]:
    for name in names:
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_PROVIDER_TOKENS:
            raise ValidationError("provider telemetry %s must be a bounded non-negative integer" % name)
        return value
    return None


def _first_number(raw: Dict, names) -> Optional[float]:
    for name in names:
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
            raise ValidationError("provider telemetry %s must be a finite non-negative number" % name)
        return float(value)
    return None


def _bounded_token_sum(current: int, value: int, label: str) -> int:
    total = current + value
    if total > MAX_PROVIDER_TOKENS:
        raise ValidationError("provider telemetry %s exceeds the supported limit" % label)
    return total


def _provider_name(value: str) -> str:
    provider = _short_text(value).lower()
    if provider not in {"anthropic", "claude", "codex", "openai"}:
        raise ValidationError("unsupported provider telemetry source: %s" % value)
    return "anthropic" if provider == "claude" else provider


def _short_text(value) -> str:
    return " ".join(str(value or "").split())[:300]
