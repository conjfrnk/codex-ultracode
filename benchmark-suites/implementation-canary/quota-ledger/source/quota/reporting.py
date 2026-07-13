from .ledger import QuotaLedger


def usage_rows(ledger: QuotaLedger, tenants: list) -> list:
    """Return caller-ordered usage rows for canonical tenant names."""
    return [{"tenant": tenant, "used": ledger.usage(tenant)} for tenant in tenants]
