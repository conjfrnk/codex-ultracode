import json
from pathlib import Path
from typing import Iterable, List

from .errors import ValidationError
from .redaction import contains_secret_like, redact_text
from .security import read_regular_text_file_no_follow, reject_symlink_path, require_no_path_escape


MAX_PACKET_ITEM_CHARS = 512
MAX_OPAQUE_PACKET_ITEM_CHARS = 64 * 1024
MAX_PACKET_ITEM_FILE_BYTES = 1024 * 1024
MAX_JSON_POINTER_CHARS = 4096
MAX_JSON_POINTER_SEGMENTS = 64


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


def clean_packet_items(
    items: Iterable[str],
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
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
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


def _sanitize_untrusted_text(text: str) -> str:
    return (
        text.replace("BEGIN_UNTRUSTED_TASK", "[begin_untrusted_task marker removed]")
        .replace("END_UNTRUSTED_TASK", "[end_untrusted_task marker removed]")
    )
