"""Halting the session when a write to the store fails, so no work goes unrecorded."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, StorageFailed
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.storage_halt import (
    FailedWrite,
    StorageErrorKind,
    StorageFault,
    halting_message,
    seq_span,
    storage_halt_explanation,
)
from orbit.protocol import STORAGE_FAILED, ErrorOccurred, EventEnvelope

UNKNOWN_DATABASE = "the store's database"
"""The database a failure names when the store didn't say which it was."""

Divert = Callable[[], Awaitable[tuple[Sequence[EventEnvelope], str | None]]]
"""Stops appending events to the log, sending them to the fallback instead; returns those sent
there so far and the open session's fallback file, or ``None`` when none could be written."""


class Halting(Protocol):
    """What a part writing to the store tells of its failures, and asks whether to write."""

    @property
    def halted(self) -> bool:
        """Whether a write to the store failed, so nothing more is written."""
        ...

    def fail(self, write: FailedWrite, error: RuntimeError) -> None:
        """Halt the session because ``write`` failed with ``error``."""
        ...


async def _nothing_diverted() -> tuple[Sequence[EventEnvelope], str | None]:
    return (), None


async def _no_work() -> None:
    pass


def fault_of(write: FailedWrite, error: RuntimeError) -> StorageFault:
    """The fault of ``write`` failing with ``error``: as the store described it, or not
    recognised when it didn't."""
    if isinstance(error, StorageFailed):
        return StorageFault(write, error.database, error.error, error.kind)
    return StorageFault(write, UNKNOWN_DATABASE, str(error), StorageErrorKind.UNKNOWN)


class StorageHalt:
    """The session's halt, once a write to the store fails: the events not yet logged go to the
    fallback through ``divert``, an ``ErrorOccurred`` explaining the halt is published with
    ``publish``, and ``stop_work`` cancels the work running, all in the background. From the
    first failure on, the session is :attr:`halted`; later failures change nothing."""

    def __init__(
        self,
        publish: Publish,
        divert: Divert = _nothing_diverted,
        stop_work: Callable[[], Awaitable[None]] = _no_work,
    ) -> None:
        self._publish = publish
        self._divert = divert
        self._stop_work = stop_work
        self._fault: StorageFault | None = None
        self._explanation: str | None = None
        self._background = Background("storage halt", publish)
        self._closed = False

    @property
    def halted(self) -> bool:
        """Whether a write to the store failed."""
        return self._fault is not None

    @property
    def message(self) -> str | None:
        """Why the session halted, and how to resume: a short note until the explanation is
        made; none while storage works."""
        if self._fault is None:
            return None
        return self._explanation or halting_message(self._fault.write)

    def fail(self, write: FailedWrite, error: RuntimeError) -> None:
        """Halt the session because ``write`` failed with ``error``, unless it's halted already;
        once closed, only mark it halted."""
        if self._fault is not None:
            return
        self._fault = fault_of(write, error)
        if not self._closed:
            self._background.start(self._halt(self._fault))

    def check(self) -> None:
        """Refuse a command, raising :class:`Refused`, while the session is halted."""
        if self.message is not None:
            raise Refused(STORAGE_FAILED, self.message)

    async def close(self) -> None:
        """Stop halting, if it's under way; a failure from now on only marks the session
        halted."""
        self._closed = True
        await self._background.close()

    async def _halt(self, fault: StorageFault) -> None:
        unsaved, written_to = await self._divert()
        span = seq_span(envelope.seq for envelope in unsaved)
        self._explanation = storage_halt_explanation(fault, span, written_to)
        await self._publish(ErrorOccurred(code=STORAGE_FAILED, message=self._explanation))
        await self._stop_work()
