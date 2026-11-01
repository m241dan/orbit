"""One turn at a time: a second turn refused while one runs, the slot shown busy while it does,
and a turn's crash passed on rather than lost with the task that ran it."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from orbit.execution.cancelling import stop

type OnBusy = Callable[[bool], None]
"""Hears whether the slot is busy: once a turn runs, and once the current turn has ended."""

type OnCrash = Callable[[BaseException], None]
"""Hears of a failure that escaped a turn."""

type StartTask = Callable[[Coroutine[Any, Any, object]], asyncio.Task[object]]
"""Makes a turn's task, in the context the caller chooses."""


class SlotBusy(Exception):
    """A turn refused as another still runs; ``refusal``, its text, says why."""

    def __init__(self, refusal: str) -> None:
        super().__init__(refusal)
        self.refusal = refusal


def crash_of(task: asyncio.Task[object]) -> BaseException | None:
    """The exception a done ``task`` ended with; none if it finished or was cancelled."""
    if task.cancelled():
        return None
    return task.exception()


def _unwatched() -> None:
    """Note an end no one watches."""


class TurnSlot:
    """Runs one turn at a time. A second turn is refused with ``refusal``, unless the turn
    running has a refusal of its own. ``on_busy`` hears the slot is busy once a turn runs, and
    free once the current turn has ended; ``on_crash`` hears of a failure that escaped a turn;
    ``start_task`` makes a turn's task, so the caller decides the context it starts in. Neither
    callback may block: a turn's end is reported from its done-callback."""

    def __init__(
        self,
        refusal: str,
        on_busy: OnBusy,
        on_crash: OnCrash,
        start_task: StartTask = asyncio.create_task,
    ) -> None:
        self._refusal = refusal
        self._busy = refusal  # What a turn is refused with while the current one runs.
        self._on_busy = on_busy
        self._on_crash = on_crash
        self._start_task = start_task
        self._turn: asyncio.Task[object] | None = None
        # Called once the slot is free again, after on_busy, to start what waits.
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
        """Run ``work`` as the slot's turn and return it, as :meth:`begin` does."""
        return self.begin(work, refusal)

    def begin(
        self, work: Coroutine[Any, Any, object], refusal: str | None = None
    ) -> asyncio.Task[object]:
        """Run ``work`` as the slot's turn and return it, unless a turn still runs: then close
        ``work`` and raise :class:`SlotBusy`, with the running turn's refusal. While this turn
        runs, another is refused with ``refusal``, if given, or else the slot's."""
        if self.running:
            work.close()
            raise SlotBusy(self._busy)
        self._busy = self._refusal if refusal is None else refusal
        turn = self._turn = self._start_task(work)
        # A callback, so it also runs if the work is cancelled before it starts.
        turn.add_done_callback(self._ended)
        # Called once the turn is running, so what it reaches sees the slot busy.
        self._on_busy(True)
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
        if crash is not None:  # A bug; nothing awaits the turn, so it's passed on here.
            self._on_crash(crash)
        # A turn started since then reports its own end; staying quiet keeps the slot busy.
        if turn is self._turn:
            self._on_busy(False)
            self.freed()
