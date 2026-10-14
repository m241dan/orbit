"""Running tasks through agents: a fake host and scripted agents stand in for MCP and the CLI."""

import asyncio

from orbit.agent.domain.agent_task import Limits
from orbit.protocol import (
    AddTask,
    AgentExited,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    ErrorOccurred,
    Event,
    ProgressNoted,
    RunTasks,
    Settings,
    TaskGaveUp,
    TasksChanged,
    TaskSpec,
    TurnEndReason,
    VerificationSkipped,
)
from tests.agent_fakes import (
    URL,
    Call,
    FakeWorkspace,
    Steps,
    board,
    replies,
    run,
    session_for,
    verified,
)


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    reply = await call("complete_task", summary="added it")
    assert "passed" in reply.text
    return "completed"


async def test_an_agent_gets_its_task_claims_done_and_passes() -> None:
    session, _, launcher = session_for(claims_done, verification=verified(Steps(True)))
    events = await asyncio.wait_for(run(session), 5)

    [launch] = launcher.launches
    agent, url, prompt, settings = launch.agent, launch.url, launch.prompt, launch.settings
    assert (agent.id, agent.role, url, settings) == (
        "agent-1",
        "coder",
        URL,
        Settings(model="opus"),
    )
    assert "agent-1" in prompt and "get_task" in prompt
    assert [t.status for t in board(events)] == ["completed"]
    assert AgentStarted(agent_id="agent-1", role="coder", task_id="1") in events
    assert AgentExited(agent_id="agent-1", reason="finished") in events
    assert AssistantMessage(turn_id="agent-1", text="on it", agent_id="agent-1") in events
    requests = [e for e in events if isinstance(e, AgentRequest)]
    assert [(r.tool, r.arguments) for r in requests] == [
        ("get_task", {}),
        ("complete_task", {"summary": "added it"}),
    ]
    assert "Add a flag." in replies(events)[0]


async def test_the_token_stops_working_once_the_agent_is_done() -> None:
    session, host, _ = session_for(claims_done, verification=verified(Steps(True)))
    await asyncio.wait_for(run(session), 5)
    reply = await host.call("token-1", "get_task")
    assert reply.is_error and "token" in reply.text


async def test_failed_checks_go_back_to_the_same_agent_to_fix() -> None:
    async def fixes_once(call: Call) -> TurnEndReason:
        await call("get_task")
        first = await call("complete_task", summary="added it")
        assert "1 test failed" in first.text and "1 of 5" in first.text
        second = await call("complete_task", summary="fixed the test")
        assert "passed" in second.text
        return "completed"

    session, _, launcher = session_for(fixes_once, verification=verified(Steps(False, True)))
    events = await asyncio.wait_for(run(session), 5)

    assert len(launcher.launches) == 1
    shown = [
        (t.status, t.attempt, t.max_attempts)
        for e in events
        if isinstance(e, TasksChanged)
        for t in e.tasks
    ]
    assert ("running", 1, 5) in shown
    assert [t.status for t in board(events)] == ["completed"]


async def keeps_claiming(call: Call) -> TurnEndReason:
    await call("get_task")
    while "another agent" not in (await call("complete_task", summary="try")).text:
        pass
    return "completed"


async def test_once_its_fixes_run_out_a_fresh_agent_takes_over() -> None:
    session, _, launcher = session_for(
        keeps_claiming,
        claims_done,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    assert [launch.agent.id for launch in launcher.launches] == ["agent-1", "agent-2"]
    first, fix = board(events)
    assert (first.status, fix.status, fix.fixes) == ("completed", "completed", first.id)
    assert fix.task.prompt.startswith("Fix the failed verification step: Unit tests")


async def test_fallbacks_stop_after_the_chain_runs_out_of_fixes() -> None:
    session, _, launcher = session_for(
        keeps_claiming,
        keeps_claiming,
        verification=verified(Steps(False, False, False, False), max_fix_attempts=1),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    assert len(launcher.launches) == 2
    assert [t.status for t in board(events)] == ["unverified", "unverified"]
    assert any(isinstance(e, ErrorOccurred) and "fix attempts" in e.message for e in events)


async def test_an_agent_that_exits_without_claiming_done_is_verified() -> None:
    async def just_works(call: Call) -> TurnEndReason:
        await call("get_task")
        return "completed"

    steps = Steps(True)
    session, _, _ = session_for(just_works, verification=verified(steps))
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == ["Unit tests"]
    assert [t.status for t in board(events)] == ["completed"]


async def test_an_agent_that_never_asks_for_its_task_errors() -> None:
    async def wanders_off(call: Call) -> TurnEndReason:
        return "completed"

    steps = Steps()
    session, _, _ = session_for(wanders_off, verification=verified(steps))
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == []
    assert [t.status for t in board(events)] == ["error"]
    assert any(isinstance(e, ErrorOccurred) and "get_task" in e.message for e in events)


async def test_a_crashing_agent_is_reported_and_its_work_still_verified() -> None:
    async def crashes(call: Call) -> TurnEndReason:
        await call("get_task")
        raise RuntimeError("claude exited with status 1")

    session, _, _ = session_for(crashes, verification=verified(Steps(True)))
    events = await asyncio.wait_for(run(session), 5)

    error = ErrorOccurred(agent_id="agent-1", message="claude exited with status 1")
    assert error in events
    assert AgentExited(agent_id="agent-1", reason="crashed") in events
    assert [t.status for t in board(events)] == ["completed"]


async def test_giving_up_ends_the_task() -> None:
    async def gives_up(call: Call) -> TurnEndReason:
        await call("get_task")
        reply = await call("give_up", reason="the spec contradicts itself")
        assert "given up" in reply.text
        return "completed"

    session, _, _ = session_for(gives_up, verification=verified(Steps()))
    events = await asyncio.wait_for(run(session), 5)

    assert (
        TaskGaveUp(agent_id="agent-1", task_id="1", reason="the spec contradicts itself") in events
    )
    assert [t.status for t in board(events)] == ["gave_up"]


async def test_agents_can_run_a_check_themselves() -> None:
    async def checks_first(call: Call) -> TurnEndReason:
        await call("get_task")
        unknown = await call("run_check", step="Lint")
        assert unknown.is_error and "Unit tests" in unknown.text
        check = await call("run_check", step="Unit tests")
        assert "failed" in check.text and "1 test failed" in check.text
        refused = await call("run_check", step="Unit tests")
        assert refused.is_error and "1 checks" in refused.text
        await call("complete_task", summary="done")
        return "completed"

    steps = Steps(False, True)
    session, _, _ = session_for(
        checks_first, verification=verified(steps), limits=Limits(max_checks=1)
    )
    events = await asyncio.wait_for(run(session), 5)
    assert steps.runs == ["Unit tests", "Unit tests"]
    assert [t.status for t in board(events)] == ["completed"]


async def test_checks_cannot_be_run_without_verification() -> None:
    async def checks(call: Call) -> TurnEndReason:
        await call("get_task")
        reply = await call("run_check", step="Unit tests")
        assert reply.is_error and "No verification" in reply.text
        assert "passed" in (await call("complete_task", summary="done")).text
        return "completed"

    session, _, _ = session_for(checks)
    events = await asyncio.wait_for(run(session), 5)
    assert [t.status for t in board(events)] == ["completed"]


async def test_progress_notes_are_published_trimmed() -> None:
    async def chatty(call: Call) -> TurnEndReason:
        await call("get_task")
        assert (await call("report_progress", note="x" * 500)).text == "Noted."
        await call("report_progress", note="  tests written  ")
        await call("complete_task", summary="done")
        return "completed"

    session, _, _ = session_for(chatty)
    events = await asyncio.wait_for(run(session), 5)
    notes = [e.note for e in events if isinstance(e, ProgressNoted)]
    assert notes == ["x" * 199 + "…", "tests written"]


async def test_unknown_tools_and_out_of_order_calls_get_error_replies() -> None:
    async def confused(call: Call) -> TurnEndReason:
        early = await call("complete_task", summary="done?")
        assert early.is_error and "get_task" in early.text
        early_give_up = await call("give_up", reason="nah")
        assert early_give_up.is_error and "get_task" in early_give_up.text
        unknown = await call("launch_rockets")
        assert unknown.is_error and "launch_rockets" in unknown.text
        await call("get_task")
        await call("complete_task", summary="done")
        return "completed"

    session, host, _ = session_for(confused)
    await asyncio.wait_for(run(session), 5)
    stranger = await host.call("token-99", "get_task")
    assert stranger.is_error


async def test_a_second_claim_while_verifying_is_refused() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()

    async def claims_twice(call: Call) -> TurnEndReason:
        await call("get_task")
        first = asyncio.ensure_future(call("complete_task", summary="one"))
        while not steps.runs:
            await asyncio.sleep(0)
        second = await call("complete_task", summary="two")
        assert second.is_error and "already" in second.text
        assert steps.gate is not None
        steps.gate.set()
        assert "passed" in (await first).text
        return "completed"

    session, _, _ = session_for(claims_twice, verification=verified(steps))
    events = await asyncio.wait_for(run(session), 5)
    assert [t.status for t in board(events)] == ["completed"]


async def test_an_agent_that_lingers_after_finishing_is_stopped() -> None:
    async def lingers(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="done")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(lingers)
    events = await asyncio.wait_for(run(session), 5)
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert [t.status for t in board(events)] == ["completed"]


async def test_cancelling_during_verification_stops_the_agent_and_the_run() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="done")
        return "completed"

    session, _, launcher = session_for(waits, claims_done, verification=verified(steps))
    for prompt in ("one", "two"):
        await session.send(AddTask(task=TaskSpec(prompt=prompt)))
    await session.send(RunTasks())
    while not steps.runs:
        await asyncio.sleep(0)
    await session.send(CancelTurn())
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if event == BusyChanged(busy=False):
            break

    assert [t.status for t in board(events)] == ["cancelled", "pending"]
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert len(launcher.launches) == 1


async def test_the_host_is_started_once_for_all_tasks() -> None:
    session, host, launcher = session_for(
        claims_done, claims_done, verification=verified(Steps(True, True))
    )
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert host.starts == 1
    assert [launch.agent.id for launch in launcher.launches] == ["agent-1", "agent-2"]
    assert [t.status for t in board(events)] == ["completed", "completed"]


async def test_an_agent_whose_turn_errors_counts_as_crashed() -> None:
    async def errors(call: Call) -> TurnEndReason:
        await call("get_task")
        return "error"

    session, _, _ = session_for(errors)
    events = await asyncio.wait_for(run(session), 5)
    assert AgentExited(agent_id="agent-1", reason="crashed") in events
    assert [t.status for t in board(events)] == ["completed"]


async def test_cancelling_a_working_agent_without_verification() -> None:
    fetched = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(works_forever)
    await session.send(AddTask(task=TaskSpec(prompt="one")))
    await session.send(RunTasks())
    await asyncio.wait_for(fetched.wait(), 5)
    await session.send(CancelTurn())
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if event == BusyChanged(busy=False):
            break
    assert [t.status for t in board(events)] == ["cancelled"]
    assert AgentExited(agent_id="agent-1", reason="killed") in events


async def stops_after_a_fix_request(call: Call) -> TurnEndReason:
    await call("get_task")
    assert "1 test failed" in (await call("complete_task", summary="done")).text
    return "completed"


async def test_an_agent_that_stops_without_changing_anything_falls_back_unverified() -> None:
    steps = Steps(False, True)
    # Before the task, after it, after the failed check, and when the agent exits.
    workspace = FakeWorkspace("before", "edited", "edited", "edited", "fix-before", "fixed")
    session, _, launcher = session_for(
        stops_after_a_fix_request, claims_done, verification=verified(steps, workspace)
    )
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == ["Unit tests", "Unit tests"]  # Once for each agent's claim.
    assert VerificationSkipped(task_id="1", reason="unchanged") in events
    assert [launch.agent.id for launch in launcher.launches] == ["agent-1", "agent-2"]
    assert [t.status for t in board(events)] == ["completed", "completed"]


async def test_an_agent_that_stops_after_changing_files_is_verified_again() -> None:
    steps = Steps(False, True)
    workspace = FakeWorkspace("before", "edited", "edited", "edited again", "edited again")
    session, _, launcher = session_for(
        stops_after_a_fix_request, verification=verified(steps, workspace)
    )
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == ["Unit tests", "Unit tests"]
    assert len(launcher.launches) == 1
    assert [t.status for t in board(events)] == ["completed"]
