def dedupe_subscriptions(names: list) -> list:
    """Return canonical event names once, preserving first appearance.

    ``names`` must be a list. Each entry follows ``canonical_event_name``'s
    contract. Invalid containers or entries surface that contract's errors.
    """
    seen = set()
    result = []
    for name in names:
        key = name.strip().lower().replace(" ", ".")
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result
