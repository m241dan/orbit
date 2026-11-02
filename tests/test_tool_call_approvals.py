"""The agents' calls held for approval: numbered on from the session's mark, published as they're
held, settled once by the user, expired after an hour with no frontend connected, withdrawn as
their caller stops waiting, and declined as the session is left or orbit closes."""

import asyncio
from datetime import timedelta

import pytest

from orbit.agent.application.consult_approvals import Timer
from orbit.agent.application.tool_call_approvals import Ruling, ToolCall, ToolCallApprovals
from orbit.agent.domain.consult_limits import HOUR
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    UNKNOWN,
    DecideToolCall,
    Event,
    ToolCallDecided,
    ToolCallDecision,
    ToolCallHeld,
)
from tests.waiting import eventually

CALL = ToolCall("agent-1", "coder", "github", "github__create_issue", {"title": "Flaky"})


def held(number: int) -> ToolCallHeld:
    return ToolCallHeld(
        call=number,
        agent_id="agent-1",
        role="coder",
        server="github",
        tool="github__create_issue",
        arguments={"title": "Flaky"},
    )


class Clock:
    """A clock that sleeping moves on, at once."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Holding:
    """Approvals over a :class:`Clock`, with what they posted and how many saves they asked
    for, and the rulings noted."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.posted: list[Event] = []
        self.saves = 0
        self.rulings: list[Ruling] = []
        self.approvals = ToolCallApprovals(
            self.posted.append, self._save, Timer(self.clock, self.clock.sleep)
        )

    async def _save(self) -> None:
        self.saves += 1

    def ask(self) -> asyncio.Task[ToolCallDecision]:
        """``CALL`` asked about, in a task of its own."""
        return asyncio.ensure_future(self.approvals.ask(CALL, self.rulings.append))

    async def asked(self, count: int = 1) -> list[asyncio.Task[ToolCallDecision]]:
        """``count`` calls asked about, once all are held."""
        tasks = [self.ask() for _ in range(count)]
        await eventually(lambda: len(self.approvals.waiting) == count, poll=0)
        return tasks


async def test_a_call_held_is_numbered_published_saved_and_waits() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = (await holding.asked())[0]
    assert holding.posted == [held(1)]
    assert holding.approvals.waiting == (held(1),)
    assert (holding.saves, holding.approvals.last, task.done()) == (1, 1, False)


async def test_calls_are_numbered_after_the_mark_of_the_session_opened() -> None:
    holding = Holding()
    holding.approvals.replace(41)
    await holding.asked()
    assert holding.approvals.waiting == (held(42),)


@pytest.mark.parametrize(("answer", "decision"), [("allow", "allowed"), ("deny", "declined")])
async def test_the_users_decision_settles_the_call_and_is_published(
    answer: str, decision: ToolCallDecision
) -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = (await holding.asked())[0]
    holding.clock.now = 30
    holding.approvals.decide(DecideToolCall.model_validate({"call": 1, "decision": answer}))
    assert await task == decision
    assert holding.posted[-1] == ToolCallDecided(call=1, decision=decision)
    assert holding.rulings == [Ruling(decision, timedelta(seconds=30))]
    assert holding.approvals.waiting == ()


async def test_a_second_decision_on_a_call_is_refused_as_unknown() -> None:
    holding = Holding()
    task = (await holding.asked())[0]
    holding.approvals.decide(DecideToolCall(call=1, decision="allow"))
    with pytest.raises(Refused) as refused:
        holding.approvals.decide(DecideToolCall(call=1, decision="deny"))
    assert refused.value.code == UNKNOWN
    assert refused.value.message == "No tool call 1 is waiting for approval."
    assert await task == "allowed"


async def test_a_decision_on_a_call_never_held_is_refused_as_unknown() -> None:
    holding = Holding()
    with pytest.raises(Refused) as refused:
        holding.approvals.decide(DecideToolCall(call=7, decision="allow"))
    assert refused.value.code == UNKNOWN


async def test_one_held_with_no_frontend_connected_expires_after_an_hour() -> None:
    holding = Holding()
    task = (await holding.asked())[0]
    assert await task == "expired"
    assert holding.clock.slept == [HOUR]
    assert holding.posted[-1] == ToolCallDecided(call=1, decision="expired")
    assert holding.rulings == [Ruling("expired", timedelta(seconds=HOUR))]


async def test_one_held_doesnt_expire_while_a_frontend_is_connected() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = (await holding.asked())[0]
    await asyncio.sleep(0)
    assert (holding.clock.slept, task.done()) == ([], False)
    task.cancel()


async def test_one_held_doesnt_expire_while_paused_and_does_once_unpaused() -> None:
    holding = Holding()
    holding.approvals.pause()
    task = (await holding.asked())[0]
    await asyncio.sleep(0)
    assert (holding.clock.slept, task.done()) == ([], False)
    holding.approvals.unpause()
    assert await task == "expired"


async def test_a_call_whose_caller_stops_waiting_is_withdrawn() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = (await holding.asked())[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert holding.posted[-1] == ToolCallDecided(call=1, decision="withdrawn")
    assert holding.rulings == [Ruling("withdrawn", timedelta(0))]
    assert holding.approvals.waiting == ()


async def test_declining_all_settles_every_call_held_as_declined() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    tasks = await holding.asked(2)
    holding.approvals.decline_all()
    assert [await task for task in tasks] == ["declined", "declined"]
    assert holding.posted[-2:] == [
        ToolCallDecided(call=1, decision="declined"),
        ToolCallDecided(call=2, decision="declined"),
    ]


async def test_another_session_opening_declines_those_still_held() -> None:
    holding = Holding()
    holding.approvals.viewers(1)
    task = (await holding.asked())[0]
    holding.approvals.replace(0)
    assert await task == "declined"
    assert holding.approvals.last == 0
