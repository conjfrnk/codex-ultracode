import json
import math
import re
import string
from pathlib import Path
from typing import Any, Iterable, List

from .errors import ValidationError
from .redaction import contains_secret_like, redact_text
from .security import read_regular_text_file_no_follow, reject_symlink_path, require_no_path_escape


MAX_PACKET_ITEM_CHARS = 512
MAX_OPAQUE_PACKET_ITEM_CHARS = 64 * 1024
MAX_JSON_PACKET_ITEM_CHARS = 64 * 1024
MAX_JSON_PACKET_ITEM_DEPTH = 16
MAX_JSON_PACKET_ITEM_NODES = 512
MAX_PACKET_ITEM_FILE_BYTES = 1024 * 1024
MAX_JSON_POINTER_CHARS = 4096
MAX_JSON_POINTER_SEGMENTS = 64
JSON_ITEM_FIELD = re.compile(
    r"^item(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$"
)


def clean_packet_item(item: str, label: str) -> str:
    if not isinstance(item, str) or not item.strip():
        raise ValidationError("%s values must be non-empty strings" % label)
    if any(char in item for char in "\r\n\0"):
        raise ValidationError("%s values must be single-line strings" % label)
    cleaned = item.strip()
    if len(cleaned) > MAX_PACKET_ITEM_CHARS:
        raise ValidationError("%s values must be at most %d characters" % (label, MAX_PACKET_ITEM_CHARS))
    if contains_secret_like(cleaned):
        raise ValidationError("%s values must not contain secret-like values" % label)
    require_no_path_escape(cleaned)
    return _sanitize_untrusted_text(redact_text(cleaned))


def clean_opaque_packet_item(item: str, label: str) -> str:
    if not isinstance(item, str) or not item.strip():
        raise ValidationError("%s values must be non-empty strings" % label)
    if "\0" in item:
        raise ValidationError("%s values must not contain NUL characters" % label)
    cleaned = item
    if len(cleaned) > MAX_OPAQUE_PACKET_ITEM_CHARS:
        raise ValidationError(
            "%s values must be at most %d characters"
            % (label, MAX_OPAQUE_PACKET_ITEM_CHARS)
        )
    if contains_secret_like(cleaned):
        raise ValidationError("%s values must not contain secret-like values" % label)
    return _sanitize_untrusted_text(redact_text(cleaned))


def clean_json_packet_item(item: Any, label: str) -> str:
    if not isinstance(item, dict) or not item:
        raise ValidationError("%s values must be non-empty JSON objects" % label)
    counter = [0]
    normalized = _normalize_json_packet_value(item, label, 0, counter)
    try:
        canonical = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "%s values must be strict JSON objects: %s"
            % (label, exc.__class__.__name__)
        )
    if len(canonical) > MAX_JSON_PACKET_ITEM_CHARS:
        raise ValidationError(
            "%s values must be at most %d canonical JSON characters"
            % (label, MAX_JSON_PACKET_ITEM_CHARS)
        )
    if contains_secret_like(canonical):
        raise ValidationError("%s values must not contain secret-like values" % label)
    return canonical


def clean_packet_items(
    items: Iterable[Any],
    label: str,
    max_items: int,
    preserve_duplicates: bool = False,
    item_semantics: str = "workspace_path",
) -> List[str]:
    cleaned_items = []
    seen = set()
    for item in items:
        if item_semantics == "workspace_path":
            cleaned = clean_packet_item(item, label)
        elif item_semantics == "opaque":
            cleaned = clean_opaque_packet_item(item, label)
        elif item_semantics == "json":
            cleaned = clean_json_packet_item(item, label)
        else:
            raise ValidationError("%s item semantics are invalid" % label)
        if preserve_duplicates or cleaned not in seen:
            cleaned_items.append(cleaned)
            seen.add(cleaned)
        if len(cleaned_items) > max_items:
            raise ValidationError("%s can be supplied at most %d times" % (label, max_items))
    if not cleaned_items:
        raise ValidationError("%s values resolved no usable items" % label)
    return cleaned_items


def read_packet_items_file(
    path: Path,
    label: str,
    max_items: int,
    preserve_duplicates: bool = False,
    item_semantics: str = "workspace_path",
) -> List[str]:
    if item_semantics == "json":
        raise ValidationError(
            "%s JSON item semantics require inline items or a strict JSON artifact pointer"
            % label
        )
    reject_symlink_path(path, label)
    text = read_regular_text_file_no_follow(path, label, MAX_PACKET_ITEM_FILE_BYTES)
    raw_items = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.lstrip().startswith("#"):
            continue
        raw_items.append(value)
        if len(raw_items) > max_items:
            raise ValidationError("%s can be supplied at most %d times" % (label, max_items))
    return clean_packet_items(
        raw_items,
        label,
        max_items,
        preserve_duplicates=preserve_duplicates,
        item_semantics=item_semantics,
    )


def read_packet_items_json_file(
    path: Path,
    label: str,
    max_items: int,
    pointer: str,
    preserve_duplicates: bool = False,
    item_semantics: str = "workspace_path",
) -> List[str]:
    reject_symlink_path(path, label)
    text = read_regular_text_file_no_follow(path, label, MAX_PACKET_ITEM_FILE_BYTES)
    try:
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("%s must contain strict JSON: %s" % (label, exc))
    value = resolve_json_pointer(document, pointer, label)
    if not isinstance(value, list):
        raise ValidationError("%s JSON pointer must resolve to an array" % label)
    if item_semantics == "json":
        if not all(isinstance(item, dict) and item for item in value):
            raise ValidationError(
                "%s JSON pointer must resolve to an array of non-empty objects"
                % label
            )
    elif not all(isinstance(item, str) for item in value):
        raise ValidationError("%s JSON pointer must resolve to a string array" % label)
    return clean_packet_items(
        value,
        label,
        max_items,
        preserve_duplicates=preserve_duplicates,
        item_semantics=item_semantics,
    )


def validate_json_pointer(pointer: str, label: str) -> str:
    if not isinstance(pointer, str):
        raise ValidationError("%s must be a JSON pointer string" % label)
    if len(pointer) > MAX_JSON_POINTER_CHARS:
        raise ValidationError("%s must be at most %d characters" % (label, MAX_JSON_POINTER_CHARS))
    if pointer and not pointer.startswith("/"):
        raise ValidationError("%s must be empty or start with /" % label)
    segments = pointer.split("/")[1:] if pointer else []
    if len(segments) > MAX_JSON_POINTER_SEGMENTS:
        raise ValidationError("%s may contain at most %d segments" % (label, MAX_JSON_POINTER_SEGMENTS))
    for segment in segments:
        _decode_json_pointer_segment(segment, label)
    return pointer


def resolve_json_pointer(document, pointer: str, label: str):
    validate_json_pointer(pointer, "%s JSON pointer" % label)
    current = document
    for raw_segment in pointer.split("/")[1:] if pointer else []:
        segment = _decode_json_pointer_segment(raw_segment, "%s JSON pointer" % label)
        if isinstance(current, dict):
            if segment not in current:
                raise ValidationError("%s JSON pointer does not exist" % label)
            current = current[segment]
            continue
        if isinstance(current, list):
            if not segment.isascii() or not segment.isdigit() or (len(segment) > 1 and segment[0] == "0"):
                raise ValidationError("%s JSON pointer has an invalid array index" % label)
            index = int(segment)
            if index >= len(current):
                raise ValidationError("%s JSON pointer array index is out of range" % label)
            current = current[index]
            continue
        raise ValidationError("%s JSON pointer traverses a scalar value" % label)
    return current


def _decode_json_pointer_segment(segment: str, label: str) -> str:
    output = []
    index = 0
    while index < len(segment):
        if segment[index] != "~":
            output.append(segment[index])
            index += 1
            continue
        if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
            raise ValidationError("%s contains an invalid ~ escape" % label)
        output.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(output)


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key %s" % key)
        value[key] = item
    return value


def _reject_json_constant(value: str):
    raise ValueError("non-standard JSON constant %s" % value)


def render_agent_map_prompt(
    template: str,
    item: str,
    index: int,
    item_semantics: str = "workspace_path",
) -> str:
    if not isinstance(template, str):
        raise ValidationError("agent_map prompt template must be a string")
    if not isinstance(item, str) or not item:
        raise ValidationError("agent_map rendered item must be a non-empty string")
    document = None
    if item_semantics == "json":
        try:
            document = json.loads(
                item,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "agent_map structured item must be canonical strict JSON: %s"
                % exc.__class__.__name__
            )
        if not isinstance(document, dict) or not document:
            raise ValidationError(
                "agent_map structured item must be a non-empty JSON object"
            )

    output = []
    try:
        parsed = string.Formatter().parse(template)
        for literal, field_name, format_spec, conversion in parsed:
            output.append(literal)
            if field_name is None:
                continue
            if conversion is not None or format_spec:
                raise ValidationError(
                    "agent_map prompt format conversions and specs are not supported"
                )
            if field_name == "index":
                output.append(str(index))
                continue
            if item_semantics != "json":
                if field_name != "item":
                    raise ValidationError(
                        "agent_map prompt may only use {item} and {index}"
                    )
                output.append(item)
                continue
            if not JSON_ITEM_FIELD.fullmatch(field_name):
                raise ValidationError(
                    "agent_map JSON prompt fields must use item or dotted item properties"
                )
            current = document
            for segment in field_name.split(".")[1:]:
                if not isinstance(current, dict) or segment not in current:
                    raise ValidationError(
                        "agent_map JSON item has no property %s"
                        % ".".join(field_name.split(".")[1:])
                    )
                current = current[segment]
            output.append(_json_prompt_text(current))
    except ValueError as exc:
        raise ValidationError(
            "agent_map prompt template is invalid: %s" % exc.__class__.__name__
        )
    return "".join(output)


def _normalize_json_packet_value(
    value: Any,
    label: str,
    depth: int,
    counter: List[int],
) -> Any:
    if depth > MAX_JSON_PACKET_ITEM_DEPTH:
        raise ValidationError(
            "%s values may be nested at most %d levels"
            % (label, MAX_JSON_PACKET_ITEM_DEPTH)
        )
    counter[0] += 1
    if counter[0] > MAX_JSON_PACKET_ITEM_NODES:
        raise ValidationError(
            "%s values may contain at most %d JSON nodes"
            % (label, MAX_JSON_PACKET_ITEM_NODES)
        )
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("%s values must not contain non-finite numbers" % label)
        return value
    if isinstance(value, str):
        if "\0" in value:
            raise ValidationError("%s values must not contain NUL characters" % label)
        return _sanitize_untrusted_text(value)
    if isinstance(value, list):
        return [
            _normalize_json_packet_value(item, label, depth + 1, counter)
            for item in value
        ]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValidationError(
                    "%s object keys must be non-empty strings" % label
                )
            if "\0" in key:
                raise ValidationError("%s object keys must not contain NUL characters" % label)
            cleaned_key = _sanitize_untrusted_text(key)
            if cleaned_key in normalized:
                raise ValidationError(
                    "%s object keys collide after marker sanitization" % label
                )
            normalized[cleaned_key] = _normalize_json_packet_value(
                item,
                label,
                depth + 1,
                counter,
            )
        return normalized
    raise ValidationError("%s values must contain only strict JSON types" % label)


def _json_prompt_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sanitize_untrusted_text(text: str) -> str:
    return (
        text.replace("BEGIN_UNTRUSTED_TASK", "[begin_untrusted_task marker removed]")
        .replace("END_UNTRUSTED_TASK", "[end_untrusted_task marker removed]")
    )
