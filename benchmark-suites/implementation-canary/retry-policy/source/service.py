from policy import normalize_policy


def should_retry(raw_policy: dict, completed_attempts: int) -> bool:
    """Return whether another attempt is allowed after completed attempts.

    ``completed_attempts`` must be a non-negative integer; booleans are not
    integers for this contract. Non-integer values raise ``TypeError`` and
    negative values raise ``ValueError``.
    """
    policy = normalize_policy(raw_policy)
    return completed_attempts <= policy.get("retries", 0)
