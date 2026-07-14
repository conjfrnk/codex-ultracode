import json
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import ValidationError
from .security import read_regular_text_file_no_follow


MAX_USAGE_IMPORT_JSON_BYTES = 1024 * 1024
MAX_USAGE_TOKENS = 10**12

INPUT_FIELDS = ("input_tokens", "prompt_tokens", "input_token_count", "prompt_token_count")
OUTPUT_FIELDS = ("output_tokens", "completion_tokens", "output_token_count", "completion_token_count")
CACHE_INPUT_FIELDS = ("cache_creation_input_tokens", "cache_read_input_tokens")
TOTAL_FIELDS = ("total_tokens", "total_token_count")
COST_USD_FIELDS = ("cost_usd", "total_cost_usd")
USAGE_FIELD_NAMES = set(INPUT_FIELDS + OUTPUT_FIELDS + CACHE_INPUT_FIELDS + TOTAL_FIELDS + COST_USD_FIELDS)
TOKEN_FIELD_NAMES = set(INPUT_FIELDS + OUTPUT_FIELDS + CACHE_INPUT_FIELDS + TOTAL_FIELDS)


@dataclass(frozen=True)
class ImportedUsage:
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


def load_usage_import(path: Path, provider: str = "auto") -> ImportedUsage:
    text = read_regular_text_file_no_follow(path, "usage artifact", MAX_USAGE_IMPORT_JSON_BYTES)
    try:
        data = json.loads(
            text,
            parse_constant=_reject_json_constant,
            parse_int=_bounded_json_integer,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError("usage artifact is not valid JSON: %s" % exc)
    except RecursionError:
        raise ValidationError("usage artifact nesting is too deep")
    return parse_usage_import(data, provider=provider)


def parse_usage_import(data, provider: str = "auto") -> ImportedUsage:
    normalized_provider = _normalize_provider(provider)
    candidates = _usage_candidates(data)
    if not candidates:
        raise ValidationError("usage artifact does not contain supported measured usage fields")

    parsed = []
    for candidate in candidates:
        usage = _usage_from_mapping(candidate, normalized_provider)
        if usage is not None:
            parsed.append(usage)
    unique = _unique_usages(parsed)
    if not unique:
        raise ValidationError("usage artifact does not contain supported measured usage fields")
    if len(unique) > 1:
        raise ValidationError("usage artifact contains multiple conflicting usage sections")
    return unique[0]


def _usage_candidates(data) -> List[Dict]:
    candidates: List[Dict] = []

    def visit(value, depth: int, inherited_costs: Dict) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            costs = dict(inherited_costs)
            for field in COST_USD_FIELDS:
                if field in value:
                    costs[field] = value[field]
            usage = value.get("usage")
            if _has_usage_field(value) and (
                _has_token_field(value) or (not isinstance(usage, dict) and not _contains_nested_token_usage(value))
            ):
                merged = dict(value)
                for field, field_value in inherited_costs.items():
                    if field not in merged:
                        merged[field] = field_value
                candidates.append(merged)
            if isinstance(usage, dict):
                merged = dict(usage)
                for field, field_value in costs.items():
                    if field not in merged:
                        merged[field] = field_value
                candidates.append(merged)
            for key, item in value.items():
                if key == "usage" and isinstance(item, dict):
                    continue
                visit(item, depth + 1, costs)
        elif isinstance(value, list):
            for item in value[:200]:
                visit(item, depth + 1, inherited_costs)

    visit(data, 0, {})
    return candidates


def _usage_from_mapping(raw: Dict, provider: str) -> Optional[ImportedUsage]:
    input_tokens = _consistent_int_alias(raw, INPUT_FIELDS, "input_tokens")
    output_tokens = _consistent_int_alias(raw, OUTPUT_FIELDS, "output_tokens")
    total_tokens = _consistent_int_alias(raw, TOTAL_FIELDS, "total_tokens")
    cost_usd = _consistent_number_alias(raw, COST_USD_FIELDS, "cost_usd")
    cache_input = _sum_ints(raw, CACHE_INPUT_FIELDS)
    if cache_input:
        input_tokens = (input_tokens or 0) + cache_input
        _validate_token_bound(input_tokens, "input_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None and cost_usd is None:
        return None
    if input_tokens is not None and output_tokens is not None:
        derived_total = input_tokens + output_tokens
        if total_tokens is None:
            total_tokens = derived_total
            _validate_token_bound(total_tokens, "total_tokens")
        elif total_tokens != derived_total:
            raise ValidationError("usage artifact total_tokens must equal input_tokens plus output_tokens")
    detected = _detect_provider(raw, provider)
    return ImportedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        provider=detected,
    )


def _unique_usages(usages: List[ImportedUsage]) -> List[ImportedUsage]:
    unique: Dict[Tuple, ImportedUsage] = {}
    for usage in usages:
        key = (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.cost_usd, usage.provider)
        unique[key] = usage
    return list(unique.values())


def _normalize_provider(provider: str) -> str:
    value = str(provider or "auto").strip().lower()
    if value not in {"auto", "generic", "openai", "anthropic", "codex", "claude"}:
        raise ValidationError("unsupported usage provider: %s" % provider)
    if value == "claude":
        return "anthropic"
    return value


def _detect_provider(raw: Dict, provider: str) -> str:
    if provider != "auto":
        return provider
    if any(field in raw for field in ("prompt_tokens", "completion_tokens")):
        return "openai"
    if any(field in raw for field in CACHE_INPUT_FIELDS):
        return "anthropic"
    return "generic"


def _has_usage_field(raw: Dict) -> bool:
    return any(field in raw for field in USAGE_FIELD_NAMES)


def _has_token_field(raw: Dict) -> bool:
    return any(field in raw for field in TOKEN_FIELD_NAMES)


def _contains_nested_token_usage(value, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        for item in value.values():
            if isinstance(item, dict):
                if _has_token_field(item):
                    return True
                usage = item.get("usage")
                if isinstance(usage, dict) and _has_token_field(usage):
                    return True
                if _contains_nested_token_usage(item, depth + 1):
                    return True
            elif isinstance(item, list):
                if _contains_nested_token_usage(item, depth + 1):
                    return True
    elif isinstance(value, list):
        for item in value[:200]:
            if _contains_nested_token_usage(item, depth + 1):
                return True
    return False


def _reject_json_constant(_value):
    raise ValidationError("usage artifact must not contain non-standard JSON constants")


def _bounded_json_integer(value: str):
    if len(value.lstrip("-")) > 64:
        return Decimal(value)
    return int(value)


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("usage artifact contains duplicate JSON keys")
        result[key] = value
    return result


def _consistent_int_alias(raw: Dict, fields: Tuple[str, ...], label: str) -> Optional[int]:
    values = []
    for field in fields:
        if field in raw:
            values.append(_nonnegative_int(raw[field], field))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValidationError("usage artifact has conflicting %s aliases" % label)
    return values[0]


def _sum_ints(raw: Dict, fields: Tuple[str, ...]) -> int:
    total = 0
    for field in fields:
        if field in raw:
            total += _nonnegative_int(raw[field], field)
            _validate_token_bound(total, field)
    return total


def _consistent_number_alias(raw: Dict, fields: Tuple[str, ...], label: str):
    values = []
    for field in fields:
        if field in raw:
            values.append(_nonnegative_number(raw[field], field))
    if not values:
        return None
    if any(float(value) != float(values[0]) for value in values[1:]):
        raise ValidationError("usage artifact has conflicting %s aliases" % label)
    return values[0]


def _nonnegative_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_USAGE_TOKENS:
        raise ValidationError("usage artifact %s must be a non-negative integer no greater than %s" % (label, MAX_USAGE_TOKENS))
    return value


def _validate_token_bound(value: int, label: str) -> None:
    if value > MAX_USAGE_TOKENS:
        raise ValidationError("usage artifact %s must be a non-negative integer no greater than %s" % (label, MAX_USAGE_TOKENS))


def _nonnegative_number(value, label: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValidationError("usage artifact %s must be a non-negative finite number" % label)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValidationError("usage artifact %s must be a non-negative finite number" % label)
    return value
