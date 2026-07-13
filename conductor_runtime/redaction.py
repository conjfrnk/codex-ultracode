import json
import re
import threading
import unicodedata
from typing import Iterable


APPROVAL_ID_PLACEHOLDER = "<approval-id>"

SECRET_PATTERNS = [
    re.compile(
        r"(?i)(\"?(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd)\"?"
        r"\s*:\s*\")([^\"]+)(\")"
    ),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd)\b"
        r"(\s*[:=]\s*)([^\s'\"<>]+)"
    ),
    re.compile(r"(?i)\b(bearer)\s+([a-z0-9._~+/=-]{12,})"),
    re.compile(r"(?i)\b(sk-[a-z0-9_-]{12,})"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]

_EXACT_SECRET_LOCK = threading.Lock()
_EXACT_SECRET_COUNTS = {}
_EXACT_SECRET_VARIANTS = ()


class ExactSecretRedactionScope:
    def __init__(self):
        self._values = set()
        self._active = False

    def __enter__(self):
        if self._active:
            raise RuntimeError("exact secret redaction scope is already active")
        self._active = True
        return self

    def add(self, values: Iterable[str]) -> None:
        if not self._active:
            raise RuntimeError("exact secret redaction scope is not active")
        additions = {
            value
            for value in values
            if isinstance(value, str) and value and value not in self._values
        }
        if not additions:
            return
        with _EXACT_SECRET_LOCK:
            self._values.update(additions)
            for value in additions:
                _EXACT_SECRET_COUNTS[value] = _EXACT_SECRET_COUNTS.get(value, 0) + 1
            _refresh_exact_secret_variants()

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        with _EXACT_SECRET_LOCK:
            for value in self._values:
                count = _EXACT_SECRET_COUNTS.get(value, 0)
                if count <= 1:
                    _EXACT_SECRET_COUNTS.pop(value, None)
                else:
                    _EXACT_SECRET_COUNTS[value] = count - 1
            _refresh_exact_secret_variants()
        self._values.clear()
        self._active = False
        return False


def redact_text(text: str) -> str:
    """Redact secret-like values while preserving enough context for debugging."""
    redacted = text
    for value in _EXACT_SECRET_VARIANTS:
        redacted = redacted.replace(value, "<redacted-secret>")
    redacted = SECRET_PATTERNS[0].sub(lambda match: match.group(1) + "<redacted>" + match.group(3), redacted)
    redacted = SECRET_PATTERNS[1].sub(lambda match: match.group(1) + match.group(2) + "<redacted>", redacted)
    redacted = SECRET_PATTERNS[2].sub(lambda match: match.group(1) + " <redacted>", redacted)
    redacted = SECRET_PATTERNS[3].sub("<redacted-openai-style-key>", redacted)
    redacted = SECRET_PATTERNS[4].sub("<redacted-private-key-block>", redacted)
    return redacted


def _refresh_exact_secret_variants() -> None:
    global _EXACT_SECRET_VARIANTS
    variants = set()
    for value in _EXACT_SECRET_COUNTS:
        variants.add(value)
        variants.add(json.dumps(value, ensure_ascii=True)[1:-1])
        variants.add(json.dumps(value, ensure_ascii=False)[1:-1])
    variants.discard("")
    _EXACT_SECRET_VARIANTS = tuple(
        sorted(variants, key=lambda candidate: (-len(candidate), candidate))
    )


def redact_terminal_text(text: str) -> str:
    """Redact secrets and make control characters inert in terminal output."""
    rendered = []
    for char in redact_text(text):
        if char == "\n":
            rendered.append(char)
            continue
        if char == "\t":
            rendered.append("    ")
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"}:
            codepoint = ord(char)
            if codepoint <= 0xFF:
                rendered.append("\\x%02x" % codepoint)
            elif codepoint <= 0xFFFF:
                rendered.append("\\u%04x" % codepoint)
            else:
                rendered.append("\\U%08x" % codepoint)
            continue
        rendered.append(char)
    return "".join(rendered)


def contains_secret_like(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def redact_lines(lines: Iterable[str]) -> str:
    return redact_text("".join(lines))


def redact_json_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            redact_text(key) if isinstance(key, str) else key: redact_json_value(item)
            for key, item in value.items()
        }
    return value


def redact_public_workflow_value(value):
    """Redact workflow JSON for exported/display surfaces, not runtime execution."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_public_workflow_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_public_workflow_value(item) for item in value]
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            public_key = redact_text(key) if isinstance(key, str) else key
            if key == "approval_id":
                redacted[public_key] = APPROVAL_ID_PLACEHOLDER
            else:
                redacted[public_key] = redact_public_workflow_value(item)
        return redacted
    return value
