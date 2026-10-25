"""Refusals: a command the rules won't carry out, and why; and what's said of errors: why each
happened, and the reports of those nothing expected."""

from collections.abc import Sequence

from orbit.protocol import UNKNOWN

STOPPING = "Orbit is stopping."
"""Why a command is refused once what would handle it is closing."""


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


def reason(error: BaseException) -> str:
    """Why ``error`` happened, as it says, a refusal's message included, or, when it says
    nothing, the name of its type, so a report of it never ends empty."""
    return str(error) or type(error).__name__


def failed_to(what: str, error: BaseException) -> str:
    """What's said when orbit fails to do ``what`` (such as "handle RunTasks") on ``error``,
    which nothing expected: "Orbit failed to <what>: <reason>"."""
    return f"Orbit failed to {what}: {reason(error)}"


def stopped_on(what: str, error: BaseException) -> str:
    """What's said when ``what`` (such as "The work lane") stops on ``error``, which nothing
    expected: "<what> stopped on an unexpected error: <reason>"."""
    return f"{what} stopped on an unexpected error: {reason(error)}"
