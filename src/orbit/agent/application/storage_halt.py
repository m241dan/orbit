"""Halting the session when a write to the store fails, so no work goes unrecorded."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import Publish, StorageFailed, StoreFailed
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

Failure = tuple[FailedWrite, StoreFailed]
"""A write to the store that failed, and its error."""

Retry = Callable[[], Awaitable[Failure | None]]
"""Retries the writes to the store a halt stopped, appending to the log again once they work;
returns the write that failed and its error, if one did."""


class Halting(Protocol):
    """What a part writing to the store tells of its failures, and asks whether to write."""

    @property
    def halted(self) -> bool:
        """Whether a write to the store failed, so nothing more is written."""
        ...

    def fail(self, write: FailedWrite, error: StoreFailed) -> None:
        """Halt the session because ``write`` failed with ``error``."""
        ...

    def check(self) -> None:
        """Refuse a command, raising :class:`Refused`, while the session is halted."""
        ...


Attempt = Callable[[], Awaitable[StoreFailed | None]]
"""Makes a write to the store; returns the error when it fails."""


class RetriedWrite(Protocol):
    """A write to the store a halt stopped, which resuming retries."""

    @property
    def write(self) -> FailedWrite:
        """Which write to the store this is."""
        ...

    async def retry(self) -> StoreFailed | None:
        """Make the write though the session is halted; returns the error when it fails."""
        ...


async def written(halt: Halting, write: FailedWrite, attempt: Attempt) -> None:
    """Make the write ``attempt`` makes, unless ``halt`` says the session is halted, halting it
    as ``write`` failing when it fails."""
    if halt.halted:
        return
    error = await attempt()
    if error is not None:
        halt.fail(write, error)


async def _nothing_diverted() -> tuple[Sequence[EventEnvelope], str | None]:
    return (), None


async def _no_work() -> None:
    pass


async def _nothing_to_retry() -> Failure | None:
    return None


@dataclass(frozen=True)
class HaltHooks:
    """What a halt does: ``divert`` sends the events not yet logged to the fallback,
    ``stop_work`` cancels the work running, and ``retry`` retries the writes as it resumes."""

    divert: Divert = _nothing_diverted
    stop_work: Callable[[], Awaitable[None]] = _no_work
    retry: Retry = _nothing_to_retry


_NO_HOOKS = HaltHooks()


def fault_of(write: FailedWrite, error: StoreFailed) -> StorageFault:
    """The fault of ``write`` failing with ``error``: as the store described it, or not
    recognised when it didn't."""
    if isinstance(error, StorageFailed):
        return StorageFault(write, error.database, error.error, error.kind)
    return StorageFault(write, UNKNOWN_DATABASE, str(error), StorageErrorKind.UNKNOWN)


class StorageHalt:
    """The session's halt, once a write to the store fails: the events not yet logged go to the
    fallback through the ``hooks``' ``divert``, an ``ErrorOccurred`` explaining the halt is
    published with ``publish``, and their ``stop_work`` cancels the work running, all in the
    background. From the first failure on, the session is :attr:`halted`; later failures change
    nothing, until :meth:`resume` lifts the halt."""

    def __init__(self, publish: Publish, hooks: HaltHooks = _NO_HOOKS) -> None:
        self._publish = publish
        self._divert = hooks.divert
        self._stop_work = hooks.stop_work
        self._retry = hooks.retry
        self._fault: StorageFault | None = None
        self._explanation: str | None = None
        self._halting = False  # The halt is under way in the background.
        self._resuming = False  # A resume is under way.
        self._lifting: list[Failure] | None = None  # The failures while the halt is lifted.
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
        return None if self._fault is None else self._message(self._fault)

    def _message(self, fault: StorageFault) -> str:
        """Why the session halted on ``fault``: a short note until the explanation is made."""
        return self._explanation or halting_message(fault.write)

    def fail(self, write: FailedWrite, error: StoreFailed) -> None:
        """Halt the session because ``write`` failed with ``error``, unless it's halted already;
        once closed, only mark it halted. While :meth:`resume` lifts the halt, the failure
        halts the session again once it's lifted."""
        if self._fault is not None:
            if self._lifting is not None:
                self._lifting.append((write, error))
            return
        self._fault = fault_of(write, error)
        if not self._closed:
            self._halting = True
            self._background.start(self._halt(self._fault))

    def log_failed(self, error: StoreFailed) -> None:
        """Halt the session, as appending to its event log failed with ``error``."""
        self.fail(FailedWrite.EVENT_LOG, error)

    def check(self) -> None:
        """Refuse a command, raising :class:`Refused`, while the session is halted."""
        if self.message is not None:
            raise Refused(STORAGE_FAILED, self.message)

    async def resume(self) -> None:
        """Retry the writes that failed and lift the halt once they work; does nothing while
        the session isn't halted. While halting or another resume is under way, or a write
        still fails, raises :class:`Refused`, the halt kept, explained afresh for the write
        that failed now. A write failing once the retried ones worked halts the session
        again."""
        if self._fault is None:
            return
        if self._under_way:
            raise Refused(STORAGE_FAILED, self._message(self._fault))
        self._resuming = True
        try:
            failed = await self._lift()
        finally:
            self._resuming = False
        self._fault, self._explanation = None, None
        if failed:
            self.fail(*failed[0])

    async def _lift(self) -> list[Failure]:
        """Retry the writes that failed; returns the failures while they were retried, or
        raises :class:`Refused`, the halt explained afresh, when a write still fails."""
        self._lifting = []
        try:
            failure = await self._retry()
        finally:
            failed, self._lifting = self._lifting, None
        if failure is not None:
            fault = fault_of(*failure)
            raise Refused(STORAGE_FAILED, await self._explain(fault))
        return failed

    @property
    def _under_way(self) -> bool:
        """Whether halting, or a resume, is under way: a resume is until its outcome is known,
        its failure explained."""
        return self._halting or self._resuming

    async def close(self) -> None:
        """Stop halting, if it's under way; a failure from now on only marks the session
        halted."""
        self._closed = True
        await self._background.close()

    async def _halt(self, fault: StorageFault) -> None:
        try:
            explanation = await self._explain(fault)
            await self._publish(ErrorOccurred(code=STORAGE_FAILED, message=explanation))
            await self._stop_work()
        finally:
            self._halting = False

    async def _explain(self, fault: StorageFault) -> str:
        """Make ``fault`` the halt's, with the events not logged sent to the fallback; returns
        the explanation, as the halt now gives it."""
        self._fault = fault
        unsaved, written_to = await self._divert()
        span = seq_span(envelope.seq for envelope in unsaved)
        self._explanation = storage_halt_explanation(fault, span, written_to)
        return self._explanation
