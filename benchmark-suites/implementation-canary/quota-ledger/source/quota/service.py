from .ledger import QuotaLedger
from .parser import parse_reservation


def reserve_quota(raw: dict, ledger: QuotaLedger) -> bool:
    """Parse an external request and reserve it in ``ledger``."""
    return ledger.reserve(parse_reservation(raw))
