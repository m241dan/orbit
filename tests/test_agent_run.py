"""Running tasks through agents: a fake host and scripted agents stand in for MCP and the CLI."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from orbit.agent import Verification, VerificationStep
from orbit.agent.application.agent_run import AgentRun, Outcome
from orbit.agent.application.desk import NOTE_LIMIT, AgentDesk, Agents, progress_note
from orbit.agent.application.ports import CallHandler, ProposalDecision, Reply
from orbit.agent.application.verifier import Verifier
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import ChecksProposal
from orbit.protocol import (
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
        verification=verified(Steps(False, False, False, False), fallback_fixes=1),
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


async def test_closing_stops_the_running_agent_and_the_host() -> None:
    fetched = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, host, _ = session_for(works_forever)
    await session.send(AddTask(task=TaskSpec(prompt="one")))
    await session.send(RunTasks())
    await asyncio.wait_for(fetched.wait(), 5)
    await asyncio.wait_for(session.close(), 5)
    await session.close()  # Closing twice is harmless.

    assert host.stops == 1
    events: list[Event] = []
    while not session._events.empty():
        events.append(session._events.get_nowait())
    assert AgentExited(agent_id="agent-1", reason="killed") in events


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
    role: str, publish: Publish = ignore, timeout_minutes: float | None = None
) -> tuple[AgentRun, "asyncio.Task[Outcome]"]:
    """A run of a task in ``role``, timing out after ``timeout_minutes``, whose agent has
    fetched its task and is working on it; the run publishes to ``publish``."""
    fetched = asyncio.Event()

    async def works(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    host = FakeHost()
    roles = {**ROLES, role: ROLES[role].model_copy(update={"timeout_minutes": timeout_minutes})}
    agents = Agents(
        host, ScriptedLauncher(host, works), lambda: "token-1", roles, proposal_review=Undecided()
    )
    tasks = TaskBoard()
    task_id = tasks.add(TaskSpec(prompt="a", role=role))
    verifier = Verifier(verified(Steps()), tasks, publish)
    agent_run = AgentRun(task_id, tasks, verifier, AgentDesk(agents, publish))
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

    agents = Agents(host, ScriptedLauncher(host), lambda: "token-1", ROLES)
    desk = AgentDesk(agents, publish)
    agent = desk.open(Target(), "coder", "1")
    reply = await desk.handle("token-1", "get_task", {})
    assert reply == Reply("Unknown or expired agent token.", is_error=True)
    [answer] = [e for e in events if isinstance(e, AgentReply)]
    assert (answer.text, answer.is_error) == (reply.text, True)


def desk_on(host: FakeHost, publish: Publish = ignore) -> AgentDesk:
    """A desk whose agents reach orbit through ``host``, publishing to ``publish``."""
    agents = Agents(host, ScriptedLauncher(host), lambda: "token-1", ROLES)
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


async def test_a_host_that_failed_to_start_is_started_again_next_time() -> None:
    host = FailingHost()
    desk = desk_on(host)
    with pytest.raises(RuntimeError, match="failed to start"):
        await desk.url()
    await asyncio.sleep(0)  # The failed start is forgotten.
    assert await desk.url() == URL
    assert host.starts == 1


async def test_stopping_after_a_failed_start_stops_nothing() -> None:
    host = FailingHost()
    desk = desk_on(host)
    with pytest.raises(RuntimeError):
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
    with pytest.raises(RuntimeError, match="failed to start"):
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
    return desk, asyncio.create_task(AgentRun(task_id, tasks, None, desk).run(None, "."))


def assert_closed_unannounced(desk: AgentDesk, events: list[Event]) -> None:
    assert desk.registry.by_token("token-1") is None
    assert desk.find("agent-1") is None
    assert not [e for e in events if isinstance(e, AgentStarted | AgentExited)]


async def test_a_run_whose_host_fails_to_start_closes_its_agent_unannounced() -> None:
    events: list[Event] = []
    desk, running = run_on(FailingHost(), events)
    with pytest.raises(RuntimeError, match="failed to start"):
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


GIT_FAILED = RuntimeError("git status failed")


class BreakingWorkspace(FakeWorkspace):
    """Reports ``states`` in turn as the tree's state, then fails as git might."""

    async def __call__(self, root: str) -> object:
        if not self.states:
            raise GIT_FAILED
        return await super().__call__(root)


async def breaking_step(step: VerificationStep, root: str) -> tuple[bool, str]:
    raise GIT_FAILED


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


async def test_a_proposal_whose_trial_breaks_gets_an_error_reply() -> None:
    events: list[Event] = []
    verification = Verification((STEP,), breaking_step)
    reply = await held_reply(
        "propose_checks", events, verification=verification, proposal_review=Undecided()
    )
    assert reply == Reply("Orbit couldn't check your work: git status failed", is_error=True)
    assert [t.status for t in board(events)] == ["error"]


async def test_a_call_orbit_fails_to_handle_is_answered_and_reported() -> None:
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
    assert ErrorOccurred(agent_id="agent-1", message=message) in events
    [answer] = [e for e in events if isinstance(e, AgentReply) and e.is_error]
    assert answer.text == message
