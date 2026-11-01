"""Command failures for candidate preview and commit. HTTP envelopes stay outside."""

from typing import Any

NOT_IMPLEMENTED = "not_implemented"


class CandidateCommandError(Exception):
    """A preview or commit refused to write. ``code`` is a public error code."""

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = {} if details is None else dict(details)
        super().__init__(code)
