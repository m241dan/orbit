"""Refusals: a command the rules won't carry out, and why."""

from collections.abc import Sequence

from orbit.protocol import UNKNOWN


class Refused(ValueError):
    """A refusal, for the reason ``code`` names (one of the refusal codes in
    :mod:`orbit.protocol`, such as ``UNKNOWN``); ``message``, its text, says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def unknown(problem: str, listing: str, choices: Sequence[str]) -> Refused:
    """The refusal of a name that isn't among ``choices``: the ``problem`` with it, then the
    ``listing`` (such as "the lanes are") naming the choices."""
    return Refused(UNKNOWN, f"{problem}; {listing} {joined(choices)}.")


def joined(names: Sequence[str]) -> str:
    """``names`` in order, as "a, b and c", or just "a" for one."""
    *rest, last = names
    return f"{', '.join(rest)} and {last}" if rest else last
