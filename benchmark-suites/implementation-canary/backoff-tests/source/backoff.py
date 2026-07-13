def retry_delay(attempt: int, base_seconds: float = 0.5, cap_seconds: float = 30.0) -> float:
    """Return a capped exponential retry delay.

    ``attempt`` is a zero-based non-negative integer. ``base_seconds`` and
    ``cap_seconds`` are positive real numbers, and the cap must be at least
    the base. Booleans are invalid for every numeric field. Invalid values
    raise ``TypeError`` for the wrong type and ``ValueError`` for an invalid
    range. The result is ``min(cap_seconds, base_seconds * 2 ** attempt)`` as
    a float.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise TypeError("attempt must be an integer")
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    for name, value in (("base_seconds", base_seconds), ("cap_seconds", cap_seconds)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("%s must be a real number" % name)
        if value <= 0:
            raise ValueError("%s must be positive" % name)
    if cap_seconds < base_seconds:
        raise ValueError("cap_seconds must be at least base_seconds")
    return float(min(cap_seconds, base_seconds * (2 ** attempt)))
