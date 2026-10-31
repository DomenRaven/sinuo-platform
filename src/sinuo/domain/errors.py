class IdempotencyConflict(Exception):
    """The same command key was used with a different canonical request."""


class StaleLease(Exception):
    """A worker has lost its right to persist a result."""


class ProviderDisabled(Exception):
    """No model transport has been enabled or implemented."""
