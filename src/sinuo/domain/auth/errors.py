"""Identity failures. HTTP status and envelopes live in the API layer."""


class AuthRequired(Exception):
    """Missing, expired, revoked, or otherwise unusable credentials."""


class ActionForbidden(Exception):
    """The principal is authenticated, but this action or request check is refused."""

    def __init__(self, action: str | None = None, *, category: str | None = None) -> None:
        self.action = action
        self.category = category
        super().__init__("forbidden")


class NotFoundOrHidden(Exception):
    """No membership, another project, or an object outside the assigned scope."""


class SourceRedacted(Exception):
    """The parent is visible and the target itself has a tombstone."""


class AccessEpochConflict(Exception):
    """An existing project's access_epoch did not move from the expected value to +1."""


class InvalidSchema(Exception):
    """The command body is not in the closed access or login schema."""
