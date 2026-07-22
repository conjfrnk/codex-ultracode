def retry_delay(attempt: int, base_seconds: float = 0.5, cap_seconds: float = 30.0) -> float:
    if not isinstance(attempt, int) or isinstance(attempt, bool):
        raise TypeError("attempt must be an integer")
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    values = (base_seconds, cap_seconds)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
        raise TypeError("delay values must be real numbers")
    if any(value <= 0 for value in values) or base_seconds > cap_seconds:
        raise ValueError("invalid delay range")
    return float(min(cap_seconds, base_seconds * pow(2, attempt)))
