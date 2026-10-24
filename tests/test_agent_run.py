"""Running tasks through agents: a fake host and scripted agents stand in for MCP and the CLI."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import pytest

from orbit.agent import Verification, VerificationStep
from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.desk import NOTE_LIMIT, AgentDesk, Agents, progress_note
from orbit.agent.application.outcomes import Outcome, cancelling, see_to_end, tree_failed
from orbit.agent.application.ports import (
    CallHandler,
    HostFailed,
    ProposalDecision,
    Reply,
    TreeFailed,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.verifier import Verifier
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.endings import BUG, STOPPED_BY_YOU
from orbit.agent.domain.verification import failure_report
from orbit.protocol import (
    AGENT_FAILED,
    CHECKS_EXHAUSTED,
    GIT_FAILED,
    INTERNAL,
    TIMED_OUT,
    AddTask,
    AgentExited,
    AgentReply,
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
from orbit.tracing import NoSpan, Span, current_span, use_span
from tests.agent_fakes import (
    ROLES,
    STEP,
    URL,
    Call,
    FailingHost,
    FakeHost,
    FakeWorkspace,
    GatedHost,
    ScriptedLauncher,
    Steps,
    board,
    ended_unverified,
    keeps_claiming,
    logged_with_traceback,
    payloads,
    queued,
    replies,
    run,
    send,
    session_for,
    verified,
)


async def passes(call: Call) -> TurnEndReason:
    """Fetches the task, claims it done, and checks the claim passed verification."""
    await call("get_task")
    reply = await call("complete_task", summary="added it")
    assert "passed" in reply.text
    return "completed"


async def test_an_agent_gets_its_task_claims_done_and_passes() -> None:
    session, _, launcher = session_for(passes, verification=verified(Steps(True)))
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


async def revoked(host: FakeHost, token: str = "token-1") -> bool:
    """Whether ``token`` no longer authenticates with ``host``."""
    reply = await host.call(token, "get_task")
    return reply.is_error and "token" in reply.text


async def test_the_token_stops_working_once_the_agent_is_done() -> None:
    session, host, _ = session_for(passes, verification=verified(Steps(True)))
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


async def test_once_its_fixes_run_out_a_fresh_agent_takes_over() -> None:
    session, _, launcher = session_for(
        keeps_claiming,
        passes,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    assert [launch.agent.id for launch in launcher.launches] == ["agent-1", "agent-2"]
    first, fix = board(events)
    assert (first.status, fix.status, fix.fixes) == ("completed", "completed", first.id)
    assert fix.task.prompt.startswith("Fix the failed verification step: Unit tests")


async def test_a_task_that_falls_back_ends_unverified_with_the_failed_step_as_its_reason() -> None:
    session, _, _ = session_for(
        keeps_claiming,
        passes,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    first = ended_unverified(events, "1")
    assert first.reason == "Unit tests failed"
    assert first.reason_detail == failure_report(STEP, "1 test failed")


async def test_a_passing_fix_clears_the_reason_its_parent_failed() -> None:
    session, _, _ = session_for(
        keeps_claiming,
        passes,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    first, _ = board(events)
    assert (first.status, first.reason, first.reason_detail) == ("completed", "", "")


async def test_fallbacks_stop_after_the_chain_runs_out_of_fixes() -> None:
    session, _, launcher = session_for(
        keeps_claiming,
        keeps_claiming,
        verification=verified(Steps(False, False, False, False), fallback_fixes=1),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    assert len(launcher.launches) == 2
    assert [t.status for t in board(events)] == ["unverified", "unverified"]
    assert [t.reason for t in board(events)] == ["Unit tests failed", "Unit tests failed"]
    codes = {e.code for e in events if isinstance(e, ErrorOccurred) and "fix attempts" in e.message}
    assert codes == {CHECKS_EXHAUSTED}


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
    message = "agent-1 exited without calling get_task."
    assert ErrorOccurred(agent_id="agent-1", code=AGENT_FAILED, message=message) in events
    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == ("error", f"Error: {message}", message)


async def test_an_errored_run_ends_with_its_first_error_not_a_later_one() -> None:
    async def errs(call: Call) -> TurnEndReason:
        return "error"

    session, _, launcher = session_for(errs)
    turn_error = "API Error: overloaded\nTry again later."
    launcher.turn_errors.append(turn_error)
    events = await asyncio.wait_for(run(session), 5)

    errors = [e.message for e in events if isinstance(e, ErrorOccurred)]
    assert errors == [turn_error, "agent-1 exited without calling get_task."]
    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == (
        "error",
        "Error: API Error: overloaded",
        turn_error,
    )


async def test_a_crashing_agent_is_reported_and_its_work_still_verified() -> None:
    async def crashes(call: Call) -> TurnEndReason:
        await call("get_task")
        raise RuntimeError("claude exited with status 1")

    session, _, _ = session_for(crashes, verification=verified(Steps(True)))
    events = await asyncio.wait_for(run(session), 5)

    error = ErrorOccurred(
        agent_id="agent-1", code=AGENT_FAILED, message="claude exited with status 1"
    )
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


async def test_giving_up_gives_the_first_line_of_the_reason_and_keeps_it_whole() -> None:
    reason = "The spec contradicts itself.\n\nIt asks for both a flag and no flags."

    async def gives_up(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("give_up", reason=reason)
        return "completed"

    session, _, _ = session_for(gives_up, verification=verified(Steps()))
    events = await asyncio.wait_for(run(session), 5)

    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == (
        "gave_up",
        "Gave up: The spec contradicts itself.",
        reason,
    )


async def test_agents_can_run_a_check_themselves() -> None:
    async def checks_first(call: Call) -> TurnEndReason:
        await call("get_task")
        unknown = await call("run_check", step="Lint")
        assert unknown.is_error and "the steps are: `Unit tests`." in unknown.text
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

    session, host, _ = session_for(lingers)
    events = await asyncio.wait_for(run(session), 5)
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert [t.status for t in board(events)] == ["completed"]
    assert await revoked(host)


async def test_cancelling_during_verification_stops_the_agent_and_the_run() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="done")
        return "completed"

    session, host, launcher = session_for(waits, passes, verification=verified(steps))
    for prompt in ("one", "two"):
        await send(session, AddTask(task=TaskSpec(prompt=prompt)))
    await send(session, RunTasks())
    while not steps.runs:
        await asyncio.sleep(0)
    await send(session, CancelTurn())
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if event == BusyChanged(busy=False):
            break

    assert [t.status for t in board(events)] == ["cancelled", "pending"]
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert len(launcher.launches) == 1
    assert await revoked(host)


async def test_the_host_is_started_once_for_all_tasks() -> None:
    session, host, launcher = session_for(passes, passes, verification=verified(Steps(True, True)))
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert host.starts == 1
    assert [launch.agent.id for launch in launcher.launches] == ["agent-1", "agent-2"]
    assert [t.status for t in board(events)] == ["completed", "completed"]


async def test_an_agent_whose_turn_errors_counts_as_crashed() -> None:
    async def errors(call: Call) -> TurnEndReason:
        await call("get_task")
        return "error"

    session, host, _ = session_for(errors)
    events = await asyncio.wait_for(run(session), 5)
    assert AgentExited(agent_id="agent-1", reason="crashed") in events
    assert [t.status for t in board(events)] == ["completed"]
    assert await revoked(host)


async def test_cancelling_a_working_agent_without_verification() -> None:
    fetched = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, host, _ = session_for(works_forever)
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    await asyncio.wait_for(fetched.wait(), 5)
    await send(session, CancelTurn())
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if event == BusyChanged(busy=False):
            break
    [task] = board(events)
    assert (task.status, task.reason, task.reason_detail) == ("cancelled", STOPPED_BY_YOU, "")
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert await revoked(host)


async def test_cancelling_while_the_agent_is_stopped_ends_the_run_as_cancelled() -> None:
    stopping, gate = asyncio.Event(), asyncio.Event()

    async def slow_to_stop(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="done")
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await gate.wait()
        raise AssertionError("unreachable")

    session, host, launcher = session_for(slow_to_stop, passes, exit_grace=0.01)
    for prompt in ("one", "two"):
        await send(session, AddTask(task=TaskSpec(prompt=prompt)))
    await send(session, RunTasks())
    await asyncio.wait_for(stopping.wait(), 5)
    await send(session, CancelTurn())
    gate.set()
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if event == BusyChanged(busy=False):
            break

    assert [t.status for t in board(events)] == ["cancelled", "pending"]
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert len(launcher.launches) == 1
    assert await revoked(host)


async def stops_after_a_fix_request(call: Call) -> TurnEndReason:
    await call("get_task")
    assert "1 test failed" in (await call("complete_task", summary="done")).text
    return "completed"


async def test_an_agent_that_stops_without_changing_anything_falls_back_unverified() -> None:
    steps = Steps(False, True)
    # Before the task, after it, after the failed check, and when the agent exits.
    workspace = FakeWorkspace("before", "edited", "edited", "edited", "fix-before", "fixed")
    session, _, launcher = session_for(
        stops_after_a_fix_request, passes, verification=verified(steps, workspace)
    )
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == ["Unit tests", "Unit tests"]  # Once for each agent's claim.
    assert VerificationSkipped(task_id="1", reason="unchanged") in events
    assert [launch.agent.id for launch in launcher.launches] == ["agent-1", "agent-2"]
    assert [t.status for t in board(events)] == ["completed", "completed"]


async def test_an_agent_that_stops_without_changing_anything_keeps_its_last_failure() -> None:
    workspace = FakeWorkspace("before", "edited", "edited", "edited", "fix-before", "fixed")
    session, _, _ = session_for(
        stops_after_a_fix_request,
        passes,
        verification=verified(Steps(False, True), workspace),
    )
    events = await asyncio.wait_for(run(session), 5)

    first = ended_unverified(events, "1")
    assert first.reason == "Unit tests failed"
    assert first.reason_detail == failure_report(STEP, "1 test failed")


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


async def test_closing_stops_the_running_agent_and_the_host() -> None:
    fetched = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, host, _ = session_for(works_forever)
    await send(session, AddTask(task=TaskSpec(prompt="one")))
    await send(session, RunTasks())
    await asyncio.wait_for(fetched.wait(), 5)
    await asyncio.wait_for(session.close(), 5)
    await session.close()  # Closing twice is harmless.

    assert host.stops == 1
    events = queued(session)
    assert AgentExited(agent_id="agent-1", reason="killed") in events
    assert await revoked(host)


async def test_closing_a_session_that_never_ran_a_task_starts_nothing() -> None:
    session, host, _ = session_for()
    await session.close()
    assert (host.starts, host.stops) == (0, 0)


def test_a_progress_note_is_stripped_and_cut_short_at_the_limit() -> None:
    assert progress_note("  reading the CLI\n") == "reading the CLI"
    assert progress_note("x" * NOTE_LIMIT) == "x" * NOTE_LIMIT
    assert progress_note("x" * (NOTE_LIMIT + 1)) == "x" * (NOTE_LIMIT - 1) + "…"


CANCELLED = Reply("The task was cancelled.", is_error=True)
LINT = {"name": "Lint", "command": ["ruff", "check"]}
Publish = Callable[[Event], Awaitable[None]]


async def ignore(event: Event) -> None:
    return None


class Undecided:
    """A proposal review that's never reached."""

    async def review(self, task_id: str, proposal: ChecksProposal) -> ProposalDecision:
        raise AssertionError("unreachable")


async def started(
    role: str,
    publish: Publish = ignore,
    timeout_minutes: float | None = None,
    verification: Verification | None = None,
) -> tuple[AgentRun, "asyncio.Task[Outcome]"]:
    """A run of a task in ``role``, timing out after ``timeout_minutes``, whose agent has
    fetched its task and is working on it; the run publishes to ``publish`` and verifies with
    ``verification``, or a step that passes."""
    fetched = asyncio.Event()

    async def works(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    host = FakeHost()
    roles = {**ROLES, role: ROLES[role].model_copy(update={"timeout_minutes": timeout_minutes})}
    agents = Agents(
        host,
        ScriptedLauncher(host, works),
        lambda: "token-1",
        roles,
        proposal_review=Undecided(),
        root=".",
    )
    tasks = TaskBoard()
    task_id = tasks.add(TaskSpec(prompt="a", role=role))
    board = Tasks(tasks, publish)
    verifier = Verifier(verification or verified(Steps()), board, publish)
    agent_run = AgentRun(task_id, board, verifier, AgentDesk(agents, publish))
    running = asyncio.create_task(agent_run.run(None, "."))
    await asyncio.wait_for(fetched.wait(), 5)
    return agent_run, running


async def cancelled_while_queued(role: str, tool: str, **arguments: Any) -> Reply:
    """The reply to a call to ``tool`` that's queued for the run loop of a run in ``role``,
    which is cancelled before the loop takes it."""
    agent_run, running = await started(role)
    call = asyncio.ensure_future(agent_run.handle(tool, arguments))
    await asyncio.sleep(0)  # The call is queued; the run loop hasn't woken to take it.
    running.cancel()
    return await asyncio.wait_for(call, 5)


async def test_a_claim_left_queued_when_the_run_is_cancelled_is_answered() -> None:
    assert await cancelled_while_queued("coder", "complete_task", summary="done") == CANCELLED


async def test_a_proposal_left_queued_when_the_run_is_cancelled_is_answered() -> None:
    assert await cancelled_while_queued("configurator", "propose_checks", steps=[LINT]) == CANCELLED


async def test_a_claim_left_queued_when_the_run_times_out_is_answered() -> None:
    runs: list[AgentRun] = []
    calls: list[asyncio.Future[Reply]] = []

    async def claims_at_the_timeout(event: Event) -> None:
        if isinstance(event, ErrorOccurred) and "ran out of time" in event.message:
            calls.append(asyncio.ensure_future(runs[0].handle("complete_task", {"summary": "x"})))
            await asyncio.sleep(0)  # The claim is queued, with the run loop stopped.

    agent_run, running = await started("coder", claims_at_the_timeout, timeout_minutes=0.005)
    runs.append(agent_run)
    outcome = await asyncio.wait_for(running, 5)
    assert outcome.status == "error"
    assert await asyncio.wait_for(calls[0], 5) == CANCELLED


async def test_a_run_that_runs_out_of_time_ends_in_error_saying_so() -> None:
    events: list[Event] = []

    async def publish(event: Event) -> None:
        events.append(event)

    _, running = await started("coder", publish, timeout_minutes=0.005)
    outcome = await asyncio.wait_for(running, 5)
    message = "agent-1 ran out of time (0.005 minutes)."
    assert (outcome.status, outcome.detail) == ("error", message)
    assert [e for e in events if isinstance(e, ErrorOccurred)] == [
        ErrorOccurred(agent_id="agent-1", code=TIMED_OUT, message=message)
    ]


RUN_ENDED = Reply("The run ended before the check finished.", is_error=True)


class HangingStep:
    """A step runner whose step runs until it's stopped; ``began`` is set once it's running."""

    def __init__(self) -> None:
        self.began = asyncio.Event()
        self.stopped = False

    async def __call__(self, step: VerificationStep, root: str) -> tuple[bool, str]:
        self.began.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped = True
        raise AssertionError("unreachable")


async def checking(
    step: HangingStep, timeout_minutes: float | None = None
) -> tuple["asyncio.Task[Outcome]", "asyncio.Future[Reply]"]:
    """A coder's run, timing out after ``timeout_minutes``, and its agent's call to run a
    check whose ``step`` is now running."""
    verification = Verification((STEP,), step)
    agent_run, running = await started(
        "coder", timeout_minutes=timeout_minutes, verification=verification
    )
    call = asyncio.ensure_future(agent_run.handle("run_check", {"step": STEP.name}))
    await asyncio.wait_for(step.began.wait(), 5)
    return running, call


async def test_a_check_still_running_when_the_run_times_out_is_stopped() -> None:
    step = HangingStep()
    running, call = await checking(step, timeout_minutes=0.005)
    outcome = await asyncio.wait_for(running, 5)
    assert step.stopped and outcome.status == "error"
    assert await asyncio.wait_for(call, 5) == RUN_ENDED


async def test_a_check_still_running_when_the_run_is_stopped_is_stopped() -> None:
    step = HangingStep()
    running, call = await checking(step)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert step.stopped
    assert await asyncio.wait_for(call, 5) == RUN_ENDED


async def test_a_check_whose_call_is_cancelled_is_stopped() -> None:
    step = HangingStep()
    running, call = await checking(step)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, 5)
    assert step.stopped and not running.done()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


class SlowToStop:
    """A step runner whose step, once stopped, keeps unwinding until ``release`` is set;
    ``began`` and ``unwinding`` say how far it got."""

    def __init__(self) -> None:
        self.began = asyncio.Event()
        self.unwinding = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, step: VerificationStep, root: str) -> tuple[bool, str]:
        self.began.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.unwinding.set()
            await self._unwind()
            raise
        raise AssertionError("unreachable")

    async def _unwind(self) -> None:
        """Wait for ``release``, through any cancellation meanwhile."""
        while not self.release.is_set():
            with suppress(asyncio.CancelledError):
                await self.release.wait()


async def test_a_check_whose_call_is_cancelled_holds_the_runs_end_until_it_has_stopped() -> None:
    step = SlowToStop()
    verification = Verification((STEP,), step)
    agent_run, running = await started("coder", verification=verification)
    call = asyncio.ensure_future(agent_run.handle("run_check", {"step": STEP.name}))
    await asyncio.wait_for(step.began.wait(), 5)
    call.cancel()
    await asyncio.wait_for(step.unwinding.wait(), 5)
    running.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    assert not running.done()
    step.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert call.cancelled()


class Target:
    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        raise AssertionError("unreachable")


async def test_a_call_whose_run_closes_while_it_is_published_is_refused() -> None:
    host = FakeHost()
    events: list[Event] = []

    async def publish(event: Event) -> None:
        events.append(event)
        if isinstance(event, AgentRequest):
            desk.close(agent)  # The run ends while the request is published.

    agents = Agents(host, ScriptedLauncher(host), lambda: "token-1", ROLES, root=".")
    desk = AgentDesk(agents, publish)
    agent = desk.open(Target(), "coder", "1")
    reply = await desk.handle("token-1", "get_task", {})
    assert reply == Reply("Unknown or expired agent token.", is_error=True)
    [answer] = [e for e in events if isinstance(e, AgentReply)]
    assert (answer.text, answer.is_error) == (reply.text, True)


class HangingTarget:
    """Answers no call: each is handled until it's cancelled."""

    def __init__(self) -> None:
        self.handling = asyncio.Event()

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        self.handling.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_cancelled_call_is_answered_with_an_error_reply() -> None:
    events: list[Event] = []

    async def publish(event: Event) -> None:
        events.append(event)

    agents = Agents(FakeHost(), ScriptedLauncher(FakeHost()), lambda: "token-1", ROLES, root=".")
    desk = AgentDesk(agents, publish)
    target = HangingTarget()
    agent = desk.open(target, "coder", "1")
    await desk.publish(AgentStarted(agent_id=agent.id, role="coder", task_id="1"))
    call = asyncio.ensure_future(desk.handle("token-1", "get_task", {}))
    await asyncio.wait_for(target.handling.wait(), 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    [request] = [e for e in events if isinstance(e, AgentRequest)]
    [answer] = [e for e in events if isinstance(e, AgentReply)]
    assert answer == AgentReply(
        agent_id=agent.id,
        request_id=request.request_id,
        text="Orbit stopped handling this call.",
        is_error=True,
    )
    [running] = desk.running
    assert running.open_requests == ()


def desk_on(host: FakeHost, publish: Publish = ignore) -> AgentDesk:
    """A desk whose agents reach orbit through ``host``, publishing to ``publish``."""
    agents = Agents(host, ScriptedLauncher(host), lambda: "token-1", ROLES, root=".")
    return AgentDesk(agents, publish)


async def test_concurrent_callers_start_the_host_once_and_share_its_url() -> None:
    host = GatedHost()
    desk = desk_on(host)
    urls = asyncio.gather(desk.url(), desk.url())
    await asyncio.wait_for(host.entered.wait(), 5)
    host.gate.set()
    assert list(await asyncio.wait_for(urls, 5)) == [URL, URL]
    assert host.starts == 1
    await desk.stop()
    await desk.stop()
    assert host.stops == 1


class SpanSeeingHost(FakeHost):
    """A host noting the span current as it starts, as uvicorn's serve task would run with."""

    def __init__(self) -> None:
        super().__init__()
        self.span: Span | None = None

    async def start(self, handler: CallHandler) -> str:
        self.span = current_span()
        return await super().start(handler)


async def test_the_host_started_inside_a_span_runs_with_no_span_current() -> None:
    host = SpanSeeingHost()
    desk = desk_on(host)
    none = current_span()
    with use_span(NoSpan("task")):
        assert await desk.url() == URL
    assert host.span is none


async def test_a_host_that_failed_to_start_is_started_again_next_time() -> None:
    host = FailingHost()
    desk = desk_on(host)
    with pytest.raises(HostFailed, match="failed to start"):
        await desk.url()
    await asyncio.sleep(0)  # The failed start is forgotten.
    assert await desk.url() == URL
    assert host.starts == 1


async def test_stopping_after_a_failed_start_stops_nothing() -> None:
    host = FailingHost()
    desk = desk_on(host)
    with pytest.raises(HostFailed):
        await desk.url()
    await desk.stop()
    assert host.stops == 0


class StoppedAsItFailsHost(FailingHost):
    """A host whose failing start has ``desk`` stopped before the desk forgets the start."""

    def __init__(self) -> None:
        super().__init__()
        self.desk: AgentDesk | None = None
        self.stopping: list[asyncio.Task[None]] = []

    async def start(self, handler: CallHandler) -> str:
        assert self.desk is not None
        self.stopping.append(asyncio.ensure_future(self.desk.stop()))
        return await super().start(handler)


async def test_stopping_as_a_start_fails_stops_nothing() -> None:
    host = StoppedAsItFailsHost()
    desk = host.desk = desk_on(host)
    with pytest.raises(HostFailed, match="failed to start"):
        await desk.url()
    [stopping] = host.stopping
    await asyncio.wait_for(stopping, 5)
    assert host.stops == 0


async def test_stopping_while_the_host_starts_cancels_the_start_and_stops_the_host() -> None:
    host = GatedHost()
    desk = desk_on(host)
    url = asyncio.ensure_future(desk.url())
    await asyncio.wait_for(host.entered.wait(), 5)
    await asyncio.wait_for(desk.stop(), 5)
    with pytest.raises(asyncio.CancelledError):
        await url
    assert (host.starts, host.stops) == (0, 1)


def run_on(host: FakeHost, events: list[Event]) -> tuple[AgentDesk, "asyncio.Task[Outcome]"]:
    """A desk on ``host`` publishing to ``events``, and a coder's run on it."""

    async def publish(event: Event) -> None:
        events.append(event)

    desk = desk_on(host, publish)
    tasks = TaskBoard()
    task_id = tasks.add(TaskSpec(prompt="a"))
    return desk, asyncio.create_task(
        AgentRun(task_id, Tasks(tasks, publish), None, desk).run(None, ".")
    )


def assert_closed_unannounced(desk: AgentDesk, events: list[Event]) -> None:
    assert desk.registry.by_token("token-1") is None
    assert desk.find("agent-1") is None
    assert not [e for e in events if isinstance(e, AgentStarted | AgentExited)]


async def test_a_run_whose_host_fails_to_start_closes_its_agent_unannounced() -> None:
    events: list[Event] = []
    desk, running = run_on(FailingHost(), events)
    with pytest.raises(HostFailed, match="failed to start"):
        await asyncio.wait_for(running, 5)
    assert_closed_unannounced(desk, events)


async def test_a_run_cancelled_while_the_host_starts_closes_its_agent_unannounced() -> None:
    events: list[Event] = []
    host = GatedHost()
    desk, running = run_on(host, events)
    await asyncio.wait_for(host.entered.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert_closed_unannounced(desk, events)
    await desk.stop()


GIT_STATUS_FAILED = RuntimeError("git status failed")


class BreakingWorkspace(FakeWorkspace):
    """Reports ``states`` in turn as the tree's state, then fails as git might."""

    async def __call__(self, root: str) -> object:
        if not self.states:
            raise GIT_STATUS_FAILED
        return await super().__call__(root)


async def breaking_step(step: VerificationStep, root: str) -> tuple[bool, str]:
    raise GIT_STATUS_FAILED


async def held_reply(tool: str, events: list[Event], **options: Any) -> Reply:
    """The reply to the agent's call to ``tool``, made in a task of its own so it outlives
    the agent, which is stopped once the run fails; ``events`` gets the session's events."""
    calls: list[asyncio.Future[Reply]] = []
    proposing = tool == "propose_checks"

    async def calls_once(call: Call) -> TurnEndReason:
        await call("get_task")
        arguments = {"steps": [LINT]} if proposing else {"summary": "done"}
        calls.append(asyncio.ensure_future(call(tool, **arguments)))
        await asyncio.shield(calls[0])
        return "completed"

    session, _, _ = session_for(calls_once, **options)
    task = TaskSpec(prompt="a", role="configurator" if proposing else "coder")
    events.extend(await asyncio.wait_for(run(session, task), 5))
    return await asyncio.wait_for(calls[0], 5)


async def test_a_claim_whose_verification_breaks_gets_an_error_reply() -> None:
    events: list[Event] = []
    verification = verified(Steps(), BreakingWorkspace("before"))
    reply = await held_reply("complete_task", events, verification=verification)
    assert reply == Reply("Orbit couldn't check your work: git status failed", is_error=True)
    assert [t.status for t in board(events)] == ["error"]


async def test_a_claim_whose_failure_cannot_be_recorded_gets_an_error_reply() -> None:
    events: list[Event] = []
    verification = verified(Steps(False), BreakingWorkspace("before", "after"))
    reply = await held_reply("complete_task", events, verification=verification)
    assert reply == Reply("Orbit couldn't check your work: git status failed", is_error=True)
    assert [t.status for t in board(events)] == ["error"]


async def test_a_proposal_whose_trial_breaks_gets_an_error_reply() -> None:
    events: list[Event] = []
    verification = Verification((STEP,), breaking_step)
    reply = await held_reply(
        "propose_checks", events, verification=verification, proposal_review=Undecided()
    )
    assert reply == Reply("Orbit couldn't check your work: git status failed", is_error=True)
    assert [t.status for t in board(events)] == ["error"]


async def test_a_call_orbit_fails_to_handle_is_answered_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    answers: list[Reply] = []

    async def checks(call: Call) -> TurnEndReason:
        await call("get_task")
        answers.append(await call("run_check", step="Unit tests"))
        await call("give_up", reason="orbit is broken")
        return "completed"

    verification = Verification((STEP,), breaking_step)
    session, _, _ = session_for(checks, verification=verification)
    events = await asyncio.wait_for(run(session), 5)
    message = "Orbit failed to handle run_check: git status failed"
    assert answers == [Reply(message, is_error=True)]
    assert ErrorOccurred(agent_id="agent-1", code=INTERNAL, message=message) in events
    [answer] = [e for e in events if isinstance(e, AgentReply) and e.is_error]
    assert answer.text == message
    assert logged_with_traceback(caplog.records, message)


def started_task() -> tuple[Tasks, str]:
    """Tasks on a new board, publishing nowhere, and the id of a task started on it."""

    async def publish(event: Event) -> None:
        pass

    tasks = Tasks(TaskBoard(), publish)
    task_id = tasks.board.add(TaskSpec(prompt="a"))
    tasks.board.set_status(task_id, "running")
    return tasks, task_id


def ending(tasks: Tasks, task_id: str) -> tuple[str, str, str]:
    state = tasks.board.get(task_id)
    return state.status, state.reason, state.reason_detail


DEFAULT = Outcome("cancelled", reason="Cancelled")


async def test_see_to_end_records_the_outcome_reached() -> None:
    tasks, task_id = started_task()

    async def reached() -> Outcome:
        return Outcome("completed")

    assert await see_to_end(tasks, task_id, DEFAULT, reached()) == Outcome("completed")
    assert ending(tasks, task_id) == ("completed", "", "")


async def test_see_to_end_records_a_bug_and_re_raises_it() -> None:
    tasks, task_id = started_task()

    async def broken() -> Outcome:
        raise KeyError("oops")

    with pytest.raises(KeyError):
        await see_to_end(tasks, task_id, DEFAULT, broken())
    assert ending(tasks, task_id) == ("error", BUG, "'oops'")


async def test_see_to_end_records_the_default_when_cancelled() -> None:
    tasks, task_id = started_task()

    async def cancelled() -> Outcome:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await see_to_end(tasks, task_id, DEFAULT, cancelled())
    assert ending(tasks, task_id) == ("cancelled", "Cancelled", "")


async def test_see_to_end_leaves_a_task_it_never_started_as_it_was() -> None:
    tasks, task_id = started_task()
    tasks.board.set_status(task_id, "pending")

    async def unstarted() -> Outcome:
        return Outcome("completed")

    assert await see_to_end(tasks, task_id, DEFAULT, unstarted()) == Outcome("completed")
    assert ending(tasks, task_id) == ("pending", "", "")


async def cancelling_once_cancelled(waiting: asyncio.Event) -> bool:
    """Whether :func:`cancelling` says so in a task cancelled as it waits, once ``waiting``."""
    waiting.set()
    with suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
    return cancelling()


async def test_a_task_being_cancelled_is_cancelling() -> None:
    waiting = asyncio.Event()
    task = asyncio.create_task(cancelling_once_cancelled(waiting))
    await waiting.wait()
    task.cancel()
    assert await task


async def test_a_task_not_being_cancelled_is_not_cancelling() -> None:
    assert not cancelling()


async def test_tree_failed_publishes_the_error_and_ends_in_it() -> None:
    published: list[Event] = []

    async def publish(event: Event) -> None:
        published.append(event)

    outcome = await tree_failed(publish, TreeFailed("git broke"))

    assert published == [ErrorOccurred(code=GIT_FAILED, message="git broke")]
    assert (outcome.status, outcome.reason, outcome.detail) == (
        "error",
        "Error: git broke",
        "git broke",
    )


@pytest.mark.parametrize("minutes", [None, 60])
async def test_a_timeout_that_is_not_the_runs_own_ends_it_in_error(minutes: float | None) -> None:
    events: list[Event] = []
    verifying = asyncio.Event()

    async def times_out_while_verifying(event: Event) -> None:
        events.append(event)
        if verifying.is_set():
            verifying.clear()  # Only once, so the run can still end.
            raise TimeoutError("pipe timed out")

    agent_run, running = await started("coder", times_out_while_verifying, minutes)
    verifying.set()
    call = asyncio.ensure_future(agent_run.handle("complete_task", {"summary": "done"}))
    outcome = await asyncio.wait_for(running, 5)
    assert (outcome.status, outcome.detail) == ("error", "pipe timed out")
    assert await call == Reply("Orbit couldn't check your work: pipe timed out", is_error=True)
    assert [e for e in events if isinstance(e, ErrorOccurred)] == [
        ErrorOccurred(agent_id="agent-1", code=AGENT_FAILED, message="pipe timed out")
    ]
