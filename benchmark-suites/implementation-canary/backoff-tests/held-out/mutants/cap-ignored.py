def retry_delay(attempt: int, base_seconds: float = 0.5, cap_seconds: float = 30.0) -> float:
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise TypeError("attempt must be an integer")
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    if isinstance(base_seconds, bool) or not isinstance(base_seconds, (int, float)):
        raise TypeError("base_seconds must be a real number")
    if isinstance(cap_seconds, bool) or not isinstance(cap_seconds, (int, float)):
        raise TypeError("cap_seconds must be a real number")
    if base_seconds <= 0 or cap_seconds <= 0 or cap_seconds < base_seconds:
        raise ValueError("invalid delay range")
    return float(base_seconds * (2 ** attempt))
