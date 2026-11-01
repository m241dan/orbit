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
from orbit.execution import background
from orbit.execution.cancelling import stop
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


def crash_of(task: asyncio.Task[object]) -> BaseException | None:
    """The exception a done ``task`` ended with; none if it finished or was cancelled."""
    if task.cancelled():
        return None
    return task.exception()


def crash_report(lane: str, exc: BaseException) -> ErrorOccurred:
    """The event telling the user that ``lane`` stopped on ``exc``, which nothing expected."""
    message = unexpected(stopped_on(f"The {lane} lane", exc), exc)
    return ErrorOccurred(code=INTERNAL, message=message)


def _unwatched() -> None:
    """Note an end no one watches."""


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


class TurnSlot:
    """Runs one turn at a time in busy lane ``lane``, which the frontend shows as busy while a
    turn runs. A second turn is refused, as busy, with ``refusal``, unless the turn running
    has a refusal of its own. Events go out through ``post``, which must not block: a turn's
    end is reported from its done-callback."""

    def __init__(self, lane: BusyLane, refusal: str, post: Callable[[Event], None]) -> None:
        self._lane: BusyLane = lane
        self._refusal = refusal
        self._busy = refusal  # What a turn is refused with while the current one runs.
        self._post = post
        self._turn: asyncio.Task[object] | None = None
        # Called once the lane is free again, after its BusyChanged, to start what waits.
        self.freed: Callable[[], None] = _unwatched

    @property
    def running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    @property
    def turn(self) -> asyncio.Task[object] | None:
        """The current or last turn; none before the first."""
        return self._turn

    async def start(
        self, work: Coroutine[Any, Any, object], refusal: str | None = None
    ) -> asyncio.Task[object]:
        """Run ``work`` as the lane's turn and return it, as :meth:`begin` does."""
        return self.begin(work, refusal)

    def begin(
        self, work: Coroutine[Any, Any, object], refusal: str | None = None
    ) -> asyncio.Task[object]:
        """Run ``work`` as the lane's turn and return it, unless a turn still runs: then close
        ``work`` and raise :class:`Refused`, with the running turn's refusal. While this turn
        runs, another is refused with ``refusal``, if given, or else the slot's."""
        if self.running:
            work.close()
            raise Refused(BUSY, self._busy)
        self._busy = self._refusal if refusal is None else refusal
        turn = self._turn = _detached(work)
        # A callback, so it also runs if the work is cancelled before it starts.
        turn.add_done_callback(self._ended)
        # Posted once the turn is running, so what it reaches sees the lane busy.
        self._post(BusyChanged(busy=True, lane=self._lane))
        return turn

    def cancel(self) -> bool:
        """Cancel the turn if it still runs; returns whether it did."""
        if self._turn is None or self._turn.done():
            return False
        self._turn.cancel()
        return True

    async def close(self) -> None:
        """Cancel the turn, if it runs, and wait for it to end; cancelled meanwhile, this is
        cancelled once it has."""
        turn = self._turn
        if turn is not None and not turn.done():
            await stop(turn)

    def _ended(self, turn: asyncio.Task[object]) -> None:
        crash = crash_of(turn)
        if crash is not None:  # A bug; nothing awaits the turn, so it's reported here.
            self._post(crash_report(self._lane, crash))
        # A turn started since then reports its own end; staying quiet keeps it shown busy.
        if turn is self._turn:
            self._post(BusyChanged(busy=False, lane=self._lane))
            self.freed()
