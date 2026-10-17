"""Roles at work: which tools an agent may call, whether it's verified, how long it may take."""

import asyncio

from orbit.protocol import AgentExited, ErrorOccurred, Event, Settings, TaskSpec, TurnEndReason
from tests.agent_fakes import ROLES, Call, Steps, board, run, session_for, verified


def statuses(events: list[Event]) -> list[str]:
    return [t.status for t in board(events)]


async def test_an_agent_is_launched_with_its_roles_instructions_and_tools() -> None:
    async def done(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="ok")
        return "completed"

    session, _, launcher = session_for(done)
    await asyncio.wait_for(run(session), 5)
    [launch] = launcher.launches
    assert launch.instructions == ROLES["coder"].instructions
    assert launch.tools == ROLES["coder"].tools


async def test_a_verified_agent_is_told_the_checks_and_that_it_may_run_them() -> None:
    briefs: list[str] = []

    async def done(call: Call) -> TurnEndReason:
        briefs.append((await call("get_task")).text)
        await call("complete_task", summary="ok")
        return "completed"

    session, _, _ = session_for(done, verification=verified(Steps()))
    await asyncio.wait_for(run(session), 5)
    assert "these checks, in order: Unit tests." in briefs[0] and "run_check" in briefs[0]


async def test_an_unverified_agent_is_told_no_checks() -> None:
    briefs: list[str] = []

    async def reviews(call: Call) -> TurnEndReason:
        briefs.append((await call("get_task")).text)
        await call("complete_task", summary="Looks fine.")
        return "completed"

    session, _, _ = session_for(verification=verified(Steps()), reviewers=(reviews,))
    await asyncio.wait_for(run(session, TaskSpec(prompt="Review", role="reviewer")), 5)
    assert "Unit tests" not in briefs[0] and "run_check" not in briefs[0]


async def test_tools_outside_the_role_are_refused_and_unverified_roles_finish_on_their_claim() -> (
    None
):
    async def reviews(call: Call) -> TurnEndReason:
        await call("get_task")
        refused = await call("run_check", step="Unit tests")
        assert refused.is_error and "reviewer" in refused.text and "run_check" in refused.text
        reply = await call("complete_task", summary="Looks fine.")
        assert "passed" not in reply.text and not reply.is_error
        return "completed"

    steps = Steps()
    session, _, _ = session_for(verification=verified(steps), reviewers=(reviews,))
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="Review", role="reviewer")), 5)
    assert steps.runs == []
    assert statuses(events) == ["completed"]


async def test_a_task_in_an_unknown_role_fails_without_an_agent() -> None:
    session, _, launcher = session_for()
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="Boo", role="ghost")), 5)
    assert launcher.launches == []
    assert ErrorOccurred(message="No role named 'ghost'.") in events
    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == (
        "error",
        "Error: No role named 'ghost'.",
        "No role named 'ghost'.",
    )


async def test_an_agent_that_runs_past_its_roles_time_limit_is_stopped() -> None:
    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    roles = {"coder": ROLES["coder"].model_copy(update={"timeout_minutes": 0.001})}
    session, _, _ = session_for(works_forever, roles=roles)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="Go", settings=Settings())), 5)
    [message] = [
        e.message for e in events if isinstance(e, ErrorOccurred) and "ran out of time" in e.message
    ]
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == ("error", f"Error: {message}", message)


async def test_without_reviews_suggestions_are_refused() -> None:
    async def suggests(call: Call) -> TurnEndReason:
        await call("get_task")
        reply = await call("suggest_task", title="Add tests", prompt="Please.")
        assert reply.is_error and "suggestions" in reply.text
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(reviewers=(suggests,))
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="Review", role="reviewer")), 5)
    assert statuses(events) == ["completed"]
