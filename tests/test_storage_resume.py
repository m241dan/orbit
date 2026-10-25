"""Resuming the session once storage works again: the writes retried in order, the log resumed,
and the turn and the reviews started only once the halt lifts."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from orbit.agent.application.keeper import Contents
from orbit.agent.application.ports import StoreFailed
from orbit.agent.application.storage_halt import HaltHooks, RetriedWrite, StorageHalt
from orbit.agent.application.storage_resume import ResumeParts, StorageResume
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.storage_halt import FailedWrite, SeqSpan
from orbit.protocol import PLANNER, Event, LogGap, Settings, Usage

CONTENTS = Contents({PLANNER: Settings()}, {}, (), None, Usage())


@dataclass
class Retried:
    """A write that, retried, fails with ``error`` if there is one, noting it ran in ``ran``."""

    write: FailedWrite
    ran: list[str]
    error: StoreFailed | None = None

    async def retry(self) -> StoreFailed | None:
        self.ran.append(self.write.value)
        return self.error


@dataclass
class FakeKeeper:
    """A keeper whose record's write is ``record``, noting the contents it was asked to hold."""

    record: Retried
    held: list[Contents] = field(default_factory=list)

    def retried(self, contents: Callable[[], Contents]) -> RetriedWrite:
        self.held.append(contents())
        return self.record


@dataclass
class FakeLog:
    """A log writer whose resume returns ``resumed``, noting it in ``ran`` and the events put."""

    ran: list[str]
    resumed: list[SeqSpan] | StoreFailed = field(default_factory=list)
    put_events: list[Event] = field(default_factory=list)

    async def resume_log(self) -> list[SeqSpan] | StoreFailed:
        self.ran.append("log")
        return self.resumed

    async def put(self, item: Event) -> None:
        self.put_events.append(item)


@dataclass
class FakeTurns:
    """Work turns noting in ``ran`` when the halt lifted."""

    ran: list[str]

    async def lifted(self) -> None:
        self.ran.append("lifted")


@dataclass
class Resuming:
    """A resume over fakes, all noting what ran in ``ran``."""

    resume: StorageResume
    halt: StorageHalt
    keeper: FakeKeeper
    log: FakeLog
    ran: list[str]


def resuming(
    saves: Callable[[list[str]], Sequence[Retried]] = lambda ran: (),
    record_error: StoreFailed | None = None,
    closing: bool = False,
) -> Resuming:
    """A resume retrying the record, failing with ``record_error`` if any, then ``saves``,
    while the session is ``closing`` if so."""
    ran: list[str] = []
    keeper = FakeKeeper(Retried(FailedWrite.SESSION_RECORD, ran, record_error))
    log = FakeLog(ran)
    halt = StorageHalt(lambda event: asyncio.sleep(0), HaltHooks(retry=lambda: resume.retry()))
    parts = ResumeParts(
        keeper,
        lambda: CONTENTS,
        saves(ran),
        log,
        FakeTurns(ran),
        lambda: ran.append("kick"),
        lambda: closing,
    )
    resume = StorageResume(halt, parts)
    return Resuming(resume, halt, keeper, log, ran)


async def halted(subject: Resuming) -> None:
    """Halt ``subject``'s session, its halting done."""
    subject.halt.fail(FailedWrite.EVENT_LOG, StoreFailed("full"))
    await asyncio.sleep(0.01)


async def test_a_retry_saves_the_record_then_the_other_saves_in_order_then_the_log() -> None:
    subject = resuming(
        lambda ran: (Retried(FailedWrite.REVIEW_COUNT, ran), Retried(FailedWrite.USAGE, ran))
    )

    assert await subject.resume.retry() is None
    assert subject.ran == [
        FailedWrite.SESSION_RECORD.value,
        FailedWrite.REVIEW_COUNT.value,
        FailedWrite.USAGE.value,
        "log",
    ]
    assert subject.keeper.held == [CONTENTS]


async def test_a_retry_stops_at_the_first_save_that_fails_and_names_it() -> None:
    full = StoreFailed("full")
    subject = resuming(
        lambda ran: (
            Retried(FailedWrite.REVIEW_COUNT, ran, full),
            Retried(FailedWrite.USAGE, ran, StoreFailed("later")),
        )
    )

    assert await subject.resume.retry() == (FailedWrite.REVIEW_COUNT, full)
    assert subject.ran == [FailedWrite.SESSION_RECORD.value, FailedWrite.REVIEW_COUNT.value]


async def test_a_retry_whose_log_fails_names_the_event_log() -> None:
    subject = resuming()
    full = StoreFailed("full")
    subject.log.resumed = full

    assert await subject.resume.retry() == (FailedWrite.EVENT_LOG, full)
    assert subject.log.put_events == []


async def test_a_retry_logs_a_gap_for_each_span_of_events_lost() -> None:
    subject = resuming()
    subject.log.resumed = [(3, 4), (7, 7)]

    assert await subject.resume.retry() is None
    assert subject.log.put_events == [
        LogGap(first_seq=3, last_seq=4),
        LogGap(first_seq=7, last_seq=7),
    ]


async def test_a_resume_whose_retry_still_fails_stays_halted_and_starts_nothing() -> None:
    subject = resuming(record_error=StoreFailed("still full"))
    await halted(subject)

    with pytest.raises(Refused):
        await subject.resume.resume()
    assert subject.halt.halted
    assert "lifted" not in subject.ran and "kick" not in subject.ran
    await subject.halt.close()


async def test_a_resume_that_works_lifts_the_halt_then_starts_the_turn_and_the_lane() -> None:
    subject = resuming()
    await halted(subject)

    await subject.resume.resume()

    assert not subject.halt.halted
    assert subject.ran == [FailedWrite.SESSION_RECORD.value, "log", "lifted", "kick"]
    await subject.halt.close()


async def test_a_resume_while_closing_starts_nothing() -> None:
    subject = resuming(closing=True)
    await halted(subject)

    await subject.resume.resume()

    assert not subject.halt.halted
    assert subject.ran == [FailedWrite.SESSION_RECORD.value, "log"]
    await subject.halt.close()
