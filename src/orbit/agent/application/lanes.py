"""What the lanes share: a failure that escapes one is reported to the user, not lost with the
background task that ran it, a busy lane's turns are run one at a time, and the session's gate
says whether work may start. A lane's work outlives whatever started it, so it starts with no
span current: it doesn't record on its starter's, such as the task that landed what it reviews."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from orbit.agent.application.failures import unexpected
from orbit.agent.application.ports import Publish
from orbit.agent.domain.refusals import Refused, stopped_on
from orbit.execution import background, turns
from orbit.protocol import BUSY, INTERNAL, BusyChanged, BusyLane, ErrorOccurred, Event
from orbit.tracing import no_span


def _never() -> bool:
    return False


def _admit() -> None:
    """Refuse nothing."""


@dataclass(frozen=True)
class SessionGate:
    """What keeps work from starting: ``halted`` tells whether the session is halted, as a write
    to the store failed, ``read_only`` whether another orbit owns the repository, and ``check``
    refuses work the user asked for, raising :class:`Refused`, while the session is halted.
    Asked afresh each time, as either may change across an ``await``."""

    halted: Callable[[], bool] = _never
    read_only: Callable[[], bool] = _never
    check: Callable[[], None] = _admit


OPEN_GATE = SessionGate()
"""A gate that never keeps work from starting."""


def crash_report(lane: str, exc: BaseException) -> ErrorOccurred:
    """The event telling the user that ``lane`` stopped on ``exc``, which nothing expected."""
    message = unexpected(stopped_on(f"The {lane} lane", exc), exc)
    return ErrorOccurred(code=INTERNAL, message=message)


def _detached[T](work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """``work`` as a task of its own that sees no span current: a task copies the context it's
    created in, and this one outlives whatever started it."""
    with no_span():
        return asyncio.create_task(work)


class Background(background.Background):
    """Runs one piece of work at a time in the background for lane ``name``, as the generic
    :class:`orbit.execution.background.Background` does: a failure that escapes the work is
    reported once with ``publish``, and the work starts with no span current."""

    def __init__(self, name: str, publish: Publish) -> None:
        async def report(exc: Exception) -> None:
            await publish(crash_report(name, exc))

        super().__init__(report, _detached)


class TurnSlot(turns.TurnSlot):
    """Runs one turn at a time in busy lane ``lane``, as the generic
    :class:`orbit.execution.turns.TurnSlot` does, posting with ``post`` that the lane is busy
    while a turn runs, which the frontend shows, and a turn's crash as the lane's. A second turn
    is refused, as busy, with ``refusal``, unless the turn running has a refusal of its own; a
    turn starts with no span current. ``post`` must not block: a turn's end is reported from its
    done-callback."""

    def __init__(self, lane: BusyLane, refusal: str, post: Callable[[Event], None]) -> None:
        def show_busy(busy: bool) -> None:
            post(BusyChanged(busy=busy, lane=lane))

        def report(exc: BaseException) -> None:
            post(crash_report(lane, exc))

        super().__init__(refusal, show_busy, report, _detached)

    def begin(
        self, work: Coroutine[Any, Any, object], refusal: str | None = None
    ) -> asyncio.Task[object]:
        """Run ``work`` as the lane's turn and return it, as the generic slot does, unless a
        turn still runs: then close ``work`` and raise :class:`Refused`, as busy, with the
        running turn's refusal."""
        try:
            return super().begin(work, refusal)
        except turns.SlotBusy as busy:
            raise Refused(BUSY, busy.refusal) from None
