import re


def normalize_slug(value: str) -> str:
    """Return a lowercase ASCII slug.

    The public contract is:
    - ``value`` must be a string; other values raise ``TypeError``.
    - Unicode letters are converted to their closest ASCII representation.
    - Every run of non-ASCII-alphanumeric characters becomes one hyphen.
    - Leading and trailing separators are removed.
    """
    return re.sub(r"\s+", "-", value.strip().lower())
