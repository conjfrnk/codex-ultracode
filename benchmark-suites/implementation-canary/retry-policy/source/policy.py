def normalize_policy(raw: dict) -> dict:
    """Return a validated canonical v2 retry policy without mutating ``raw``.

    The accepted input contracts are:

    - v1 (``version`` omitted or ``1``): ``timeout_ms`` is a positive number,
      ``retries`` is a non-negative integer, and optional ``tags`` is a
      comma-separated string.
    - v2 (``version`` is ``2``): ``timeout_seconds`` is a positive number,
      ``max_attempts`` is a positive integer, and optional ``tags`` is a list
      of strings.

    Booleans are not numbers for this contract. Unknown keys, mixed-version
    keys, malformed tags, and unsupported versions raise ``ValueError``.
    Non-dict input raises ``TypeError``.

    The returned dictionary has exactly ``version``, ``timeout_seconds``,
    ``max_attempts``, and ``tags``. V1 retries count retries after the initial
    attempt, so canonical ``max_attempts`` is ``retries + 1``. Tags are
    stripped, empty tags are discarded, and duplicates are removed while
    preserving first appearance.
    """
    return dict(raw)
