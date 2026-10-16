"""What the lanes share: a failure that escapes one is reported to the user, not lost with the
background task that ran it."""

import asyncio

from orbit.protocol import ErrorOccurred


def crash_of(task: "asyncio.Task[object]") -> BaseException | None:
    """The exception a done ``task`` ended with; none if it finished or was cancelled."""
    if task.cancelled():
        return None
    return task.exception()


def crash_report(lane: str, exc: BaseException) -> ErrorOccurred:
    """The event telling the user that ``lane`` stopped on ``exc``, which nothing expected."""
    return ErrorOccurred(message=f"The {lane} lane stopped on an unexpected error: {exc!r}")
