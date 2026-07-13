from .model import Reservation


def reservation_record(reservation: Reservation, accepted: bool) -> dict:
    """Return a stable audit record without exposing ledger internals."""
    return {
        "tenant": reservation.tenant,
        "request_id": reservation.request_id,
        "units": reservation.units,
        "accepted": bool(accepted),
    }
