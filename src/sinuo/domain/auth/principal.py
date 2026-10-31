from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """Server-side subject. Roles and grant lists are not part of the principal."""

    account_id: str
    session_id: str
    auth_epoch: int
