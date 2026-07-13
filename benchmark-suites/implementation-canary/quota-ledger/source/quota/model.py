from dataclasses import dataclass


@dataclass(frozen=True)
class Reservation:
    tenant: str
    request_id: str
    units: int
