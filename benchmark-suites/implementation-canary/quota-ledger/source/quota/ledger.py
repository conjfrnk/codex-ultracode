from .model import Reservation


class QuotaLedger:
    """In-memory idempotent quota reservation ledger.

    Limits map canonical tenant names to positive integer unit limits. A
    request id is idempotent within its tenant: repeating the identical
    request returns its first decision without changing usage. Reusing that
    id with different units raises ``ValueError``. Unknown tenants raise
    ``ValueError``. A reservation succeeds when its units fit at or below the
    remaining limit, and rejected requests are idempotent too.
    """

    def __init__(self, limits: dict):
        self._limits = dict(limits)
        self._used = {tenant: 0 for tenant in limits}

    def reserve(self, reservation: Reservation) -> bool:
        limit = self._limits.get(reservation.tenant)
        if limit is None:
            return False
        if self._used[reservation.tenant] + reservation.units >= limit:
            return False
        self._used[reservation.tenant] += reservation.units
        return True

    def usage(self, tenant: str) -> int:
        """Return current usage for a canonical tenant or raise ``ValueError``."""
        return self._used[tenant]
