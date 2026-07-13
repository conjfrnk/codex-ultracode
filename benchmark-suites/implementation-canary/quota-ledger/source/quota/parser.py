from .model import Reservation


def parse_reservation(raw: dict) -> Reservation:
    """Validate and normalize an external reservation request.

    ``raw`` must be a dictionary with exactly ``tenant``, ``request_id``, and
    ``units``. Tenant and request id must be non-empty strings after trimming;
    tenant is case-folded while request-id case is preserved. Units must be a
    positive integer and not a boolean. Wrong container/value types raise
    ``TypeError``; missing, unknown, empty, or out-of-range values raise
    ``ValueError``. Caller input is never mutated.
    """
    return Reservation(raw["tenant"], raw["request_id"], raw["units"])
