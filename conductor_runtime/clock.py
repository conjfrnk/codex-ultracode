from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return naive UTC for compatibility with existing persisted timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_from_timestamp(timestamp: float) -> datetime:
    """Convert a POSIX timestamp to naive UTC without deprecated APIs."""
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
