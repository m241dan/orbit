"""Resuming the session once storage works again: retrying the writes a halt stopped, then
starting what waited for the halt to lift."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orbit.agent.application.keeper import Contents
from orbit.agent.application.ports import StoreFailed
from orbit.agent.application.storage_halt import Failure, RetriedWrite, StorageHalt
from orbit.agent.domain.storage_halt import FailedWrite, SeqSpan
from orbit.protocol import Event, LogGap


class Records(Protocol):
    """The keeper saving the session record, as resuming retries its write."""

    def retried(self, contents: Callable[[], Contents]) -> RetriedWrite: ...


class ResumedLog(Protocol):
    """The log writer, as resuming appends to the event log again."""

    async def resume_log(self) -> list[SeqSpan] | StoreFailed: ...

    async def put(self, item: Event) -> None: ...


class Lifted(Protocol):
    """The work turns, as they start the run that waited for the halt to lift."""

    async def lifted(self) -> None: ...


@dataclass(frozen=True)
class ResumeParts:
    """What resuming acts on: the ``keeper`` saving the session record holding what
    ``contents`` says, the other ``saves`` retried after it, in order, the ``log`` writer
    appended to again, the ``turns`` starting the run that waited, ``kick``, which starts the
    reviews waiting and the rest of the background the halt kept from starting, and
    ``closing``, which says whether the session is closing."""

    keeper: Records
    contents: Callable[[], Contents]
    saves: Sequence[RetriedWrite]
    log: ResumedLog
    turns: Lifted
    kick: Callable[[], None]
    closing: Callable[[], bool]


async def retried(writes: Sequence[RetriedWrite]) -> Failure | None:
    """Retry ``writes`` in order, stopping at the first that fails; returns that write and its
    error, if one did."""
    for each in writes:
        error = await each.retry()
        if error is not None:
            return each.write, error
    return None


class StorageResume:
    """Resumes the session ``halt`` halted, with the parts in ``parts``: :meth:`retry` is what
    the halt retries as it resumes, and :meth:`resume` lifts the halt and starts the turn and
    the reviews that waited."""

    def __init__(self, halt: StorageHalt, parts: ResumeParts) -> None:
        self._halt = halt
        self._parts = parts

    async def retry(self) -> Failure | None:
        """Retry the writes to the store a halt stopped: the session record as it is now, with a
        saved session's rename whose save failed, then the other saves, then appending to the
        event log, which logs a ``LogGap`` for each span of events lost; returns the write that
        failed and its error, if one did."""
        record = self._parts.keeper.retried(self._parts.contents)
        saves = await retried((record, *self._parts.saves))
        return saves or await self._resume_log()

    async def _resume_log(self) -> Failure | None:
        """Append to the event log again, publishing a ``LogGap`` for each span of events that
        reached neither it nor a fallback file; returns the error, if appending failed."""
        resumed = await self._parts.log.resume_log()
        if isinstance(resumed, StoreFailed):
            return FailedWrite.EVENT_LOG, resumed
        for first, last in resumed:
            await self._parts.log.put(LogGap(first_seq=first, last_seq=last))
        return None

    async def resume(self) -> None:
        """Lift the halt, if the writes that failed now work, then start the run whose wait to
        start again ended while halted and ``kick`` the rest; raises :class:`Refused` while the
        halt is kept. Closing meanwhile, nothing is started."""
        await self._halt.resume()
        if not self._halt.halted and not self._parts.closing():
            await self._parts.turns.lifted()
            self._parts.kick()
