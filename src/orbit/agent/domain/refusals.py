"""Refusals: a command the rules won't carry out, and why."""


class Refused(ValueError):
    """A refusal, for the reason ``code`` names (one of the refusal codes in
    :mod:`orbit.protocol`, such as ``UNKNOWN``); ``message``, its text, says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
