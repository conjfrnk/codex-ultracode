import re


def canonical_event_name(value: str) -> str:
    """Return the canonical dotted event name.

    Names are non-empty strings made of Unicode alphanumeric segments. Runs
    of whitespace, dots, hyphens, or underscores separate segments. Segment
    text is case-folded, leading and trailing separators are ignored, and any
    other punctuation raises ``ValueError``. Non-string input raises
    ``TypeError``.
    """
    return re.sub(r"\s+", ".", value.strip().lower())
