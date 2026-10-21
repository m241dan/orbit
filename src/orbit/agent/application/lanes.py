"""What the lanes share: a failure that escapes one is reported to the user, not lost with the
background task that ran it, and a busy lane's turns are run one at a time."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from orbit.agent.application.ports import Publish
from orbit.agent.domain.refusals import Refused
from orbit.protocol import BUSY, INTERNAL, BusyChanged, BusyLane, ErrorOccurred, Event


def crash_of(task: "asyncio.Task[object]") -> BaseException | None:
    """The exception a done ``task`` ended with; none if it finished or was cancelled."""
    if task.cancelled():
        return None
    return task.exception()


def crash_report(lane: str, exc: BaseException) -> ErrorOccurred:
    """The event telling the user that ``lane`` stopped on ``exc``, which nothing expected."""
    message = f"The {lane} lane stopped on an unexpected error: {exc!r}"
    return ErrorOccurred(code=INTERNAL, message=message)


def being_cancelled() -> bool:
    """Whether the task running now is being cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def stop(task: "asyncio.Task[Any]") -> None:
    """Cancel ``task`` and wait for it to end; cancelled meanwhile, this is cancelled once it
    has."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # The task's own cancel ends here; one meant for the caller goes on.
        if being_cancelled():
            raise


def _unwatched() -> None:
    """Note an end no one watches."""


class Background:
    """Runs one piece of work at a time in the background for lane ``name``. A failure that
    escapes the work is reported once with ``publish``, as it happens, and never raised;
    :attr:`ended` is called once the work has stopped."""

    def __init__(self, name: str, publish: Publish) -> None:
        self._name = name
        self._publish = publish
        self._task: asyncio.Task[None] | None = None
        self.ended: Callable[[], None] = _unwatched

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, work: Coroutine[Any, Any, object]) -> None:
        """Run ``work`` in the background, unless earlier work still runs: then drop it."""
        if self.running:
            work.close()
            return
        self._task = asyncio.create_task(self._guarded(work))
        # Work cancelled before it starts is closed, not left never awaited.
        self._task.add_done_callback(lambda _: work.close())
        self._task.add_done_callback(lambda _: self.ended())

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        """Cancel the work, if any, and wait for it to stop; cancelled meanwhile, this is
        cancelled once it has."""
        if self._task is not None:
            await stop(self._task)

    async def _guarded(self, work: Coroutine[Any, Any, object]) -> None:
        try:
            await work
        except Exception as exc:  # A bug; nothing else awaits the work, so say so here.
            await self._publish(crash_report(self._name, exc))


class TurnSlot:
    """Runs one turn at a time in busy lane ``lane``, which the frontend shows as busy while a
    turn runs. A second turn is refused, as busy, with ``refusal``. Events go out through
    ``post``, which must not block: a turn's end is reported from its done-callback."""

    def __init__(self, lane: BusyLane, refusal: str, post: Callable[[Event], None]) -> None:
        self._lane: BusyLane = lane
        self._refusal = refusal
        self._post = post
        self._turn: asyncio.Task[object] | None = None

    @property
    def running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    @property
    def turn(self) -> "asyncio.Task[object] | None":
        """The current or last turn; none before the first."""
        return self._turn

    async def start(self, work: Coroutine[Any, Any, object]) -> None:
        """Run ``work`` as the lane's turn, unless a turn still runs: then close ``work`` and
        raise :class:`Refused`."""
        if self.running:
            work.close()
            raise Refused(BUSY, self._refusal)
        self._turn = asyncio.create_task(work)
        # A callback, so it also runs if the work is cancelled before it starts.
        self._turn.add_done_callback(self._ended)
        # Posted once the turn is running, so what it reaches sees the lane busy.
        self._post(BusyChanged(busy=True, lane=self._lane))

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

    def _ended(self, turn: "asyncio.Task[object]") -> None:
        crash = crash_of(turn)
        if crash is not None:  # A bug; nothing awaits the turn, so it's reported here.
            self._post(crash_report(self._lane, crash))
        # A turn started since then reports its own end; staying quiet keeps it shown busy.
        if turn is self._turn:
            self._post(BusyChanged(busy=False, lane=self._lane))
