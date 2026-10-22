"""Recording each finished turn's usage in the background, halting the session when it can't."""

from datetime import timedelta

from orbit.agent.application.ports import AgentLaunch
from orbit.agent.application.usage_recorder import UsageRecorder
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.usage_totals import UsageRow
from orbit.protocol import Event, Settings, Usage
from tests.agent_fakes import START, FakeHalt, FakeLedger, Ticks, settled

LAUNCH = AgentLaunch(
    Agent("agent-1", "coder", "1", "token-1"), "url", "Go.", Settings(model="opus"), ".", "", ()
)
USAGE = Usage(input_tokens=10, cost_usd=0.5, turns=1, duration_ms=900)


async def unpublished(event: Event) -> None:
    raise AssertionError(f"Nothing should be published: {event}")


def recorder_for(ledger: FakeLedger | None) -> tuple[UsageRecorder, FakeHalt, Ticks]:
    """A recorder over ``ledger``, telling a halt of its failures, timed by a :class:`Ticks`."""
    halt, clock = FakeHalt(), Ticks()
    return UsageRecorder(ledger, halt, unpublished, clock), halt, clock


def row_at(minutes: int) -> UsageRow:
    """The row of a turn of :data:`LAUNCH`'s agent that cost :data:`USAGE`, ``minutes`` after
    :data:`START`."""
    return UsageRow(
        "w",
        "s",
        "coder",
        "opus",
        Usage(input_tokens=10, cost_usd=0.5),
        START + timedelta(minutes=minutes),
    )


async def test_without_a_ledger_nothing_is_recorded_nor_timed() -> None:
    recorder, halt, clock = recorder_for(None)
    recorder.record("w", "s", LAUNCH, USAGE)
    await settled()
    assert await recorder.retry() is None
    assert (clock.reads, halt.failures) == (0, [])
    await recorder.close()


async def test_a_turn_is_recorded_with_its_role_model_tokens_cost_and_time() -> None:
    ledger = FakeLedger()
    recorder, _, _ = recorder_for(ledger)
    recorder.record("w", "s", LAUNCH, USAGE)
    recorder.record("w", "s", LAUNCH, USAGE)
    await settled()
    assert ledger.rows == [row_at(0), row_at(1)]
    await recorder.close()


async def test_a_row_that_cant_be_written_halts_and_is_written_on_retrying() -> None:
    ledger = FakeLedger()
    ledger.failure = "disk full"
    recorder, halt, _ = recorder_for(ledger)
    recorder.record("w", "s", LAUNCH, USAGE)
    await settled()
    ledger.failure = None
    recorder.record("w", "s", LAUNCH, USAGE)
    await settled()
    assert (halt.failures, ledger.rows) == ([(FailedWrite.USAGE, "disk full")], [])

    assert await recorder.retry() is None
    assert ledger.rows == [row_at(0), row_at(1)]
    await recorder.close()


async def test_retrying_while_the_ledger_still_fails_returns_its_error() -> None:
    ledger = FakeLedger()
    ledger.failure = "disk full"
    recorder, _, _ = recorder_for(ledger)
    recorder.record("w", "s", LAUNCH, USAGE)
    await settled()
    error = await recorder.retry()
    assert str(error) == "disk full"
    await recorder.close()
