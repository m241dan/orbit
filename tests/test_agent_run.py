"""Running tasks through agents: a fake host and scripted agents stand in for MCP and the CLI."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from orbit.agent import Agents, AgentSession, Verification, VerificationStep
from orbit.agent.application.ports import CallHandler, Reply
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.agents import Agent
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
    SettingsChanged,
    TaskGaveUp,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    VerificationChanged,
)

URL = "http://orbit.test/mcp"
Call = Callable[..., Awaitable[Reply]]
Script = Callable[[Call], Awaitable[TurnEndReason]]


class FakeHost:
    def __init__(self) -> None:
        self.handler: CallHandler | None = None
        self.starts = 0

    async def start(self, handler: CallHandler) -> str:
        self.starts += 1
        self.handler = handler
        return URL

    async def call(self, token: str, tool: str, **arguments: Any) -> Reply:
        assert self.handler is not None
        return await self.handler(token, tool, arguments)


class ScriptedLauncher:
    """Each launch plays the next script as the agent, calling orbit through the host."""

    def __init__(self, host: FakeHost, *scripts: Script) -> None:
        self.host = host
        self.scripts = list(scripts)
        self.launches: list[tuple[Agent, str, str, Settings]] = []

    async def run_agent(
        self, agent: Agent, url: str, prompt: str, settings: Settings
    ) -> AsyncIterator[Event]:
        self.launches.append((agent, url, prompt, settings))
        script = self.scripts.pop(0)
        yield TurnStarted(turn_id=agent.id, agent_id=agent.id)
        yield AssistantMessage(turn_id=agent.id, text="on it", agent_id=agent.id)

        async def call(tool: str, **arguments: Any) -> Reply:
            return await self.host.call(agent.token, tool, **arguments)

        reason = await script(call)
        yield TurnFinished(turn_id=agent.id, reason=reason, agent_id=agent.id)


class Steps:
    """A step runner whose step passes or fails by a script of results, recording runs."""

    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.runs: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, step: VerificationStep) -> tuple[bool, str]:
        self.runs.append(step.name)
        if self.gate is not None:
            await self.gate.wait()
        passed = self.results.pop(0)
        return passed, "all good" if passed else "1 test failed"


STEP = VerificationStep(name="Unit tests", command=("pytest",))


def session_for(
    *scripts: Script,
    steps: Steps | None = None,
    limits: Limits | None = None,
    max_fix_attempts: int = 3,
    exit_grace: float = 1.0,
) -> tuple[AgentSession, FakeHost, ScriptedLauncher]:
    host = FakeHost()
    launcher = ScriptedLauncher(host, *scripts)
    tokens = iter(f"token-{n}" for n in range(1, 100))
    agents = Agents(host, launcher, lambda: next(tokens), limits or Limits(), exit_grace)
    verification = (
        Verification((STEP,), steps, max_fix_attempts=max_fix_attempts) if steps else None
    )
    session = AgentSession(object(), verification=verification, agents=agents)  # type: ignore[arg-type]
    return session, host, launcher


async def run(session: AgentSession, *tasks: TaskSpec) -> list[Event]:
    """Queue ``tasks``, run them, and return the events until the session goes idle."""
    for task in tasks or (TaskSpec(prompt="Add a flag.", settings=Settings(model="opus")),):
        await session.send(AddTask(task=task))
    await session.send(RunTasks())
    events: list[Event] = []
    async for event in session.events():
        if isinstance(event, SettingsChanged | VerificationChanged):
            continue
        events.append(event)
        if event == BusyChanged(busy=False):
            return events
    raise AssertionError("unreachable")


def board(events: list[Event]) -> tuple[TaskState, ...]:
    return [e for e in events if isinstance(e, TasksChanged)][-1].tasks


def replies(events: list[Event]) -> list[str]:
    return [e.text for e in events if isinstance(e, AgentReply)]


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    reply = await call("complete_task", summary="added it")
    assert "passed" in reply.text
    return "completed"


async def test_an_agent_gets_its_task_claims_done_and_passes() -> None:
    session, _, launcher = session_for(claims_done, steps=Steps(True))
    events = await asyncio.wait_for(run(session), 5)

    [(agent, url, prompt, settings)] = launcher.launches
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
    session, host, _ = session_for(claims_done, steps=Steps(True))
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

    session, _, launcher = session_for(fixes_once, steps=Steps(False, True))
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
        steps=Steps(False, False, True),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 5)

    assert [a.id for a, *_ in launcher.launches] == ["agent-1", "agent-2"]
    first, fix = board(events)
    assert (first.status, fix.status, fix.fixes) == ("completed", "completed", first.id)
    assert fix.task.prompt.startswith("Fix the failed verification step: Unit tests")


async def test_fallbacks_stop_after_the_chain_runs_out_of_fixes() -> None:
    session, _, launcher = session_for(
        keeps_claiming,
        keeps_claiming,
        steps=Steps(False, False, False, False),
        limits=Limits(max_attempts=1),
        max_fix_attempts=1,
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
    session, _, _ = session_for(just_works, steps=steps)
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == ["Unit tests"]
    assert [t.status for t in board(events)] == ["completed"]


async def test_an_agent_that_never_asks_for_its_task_errors() -> None:
    async def wanders_off(call: Call) -> TurnEndReason:
        return "completed"

    steps = Steps()
    session, _, _ = session_for(wanders_off, steps=steps)
    events = await asyncio.wait_for(run(session), 5)

    assert steps.runs == []
    assert [t.status for t in board(events)] == ["error"]
    assert any(isinstance(e, ErrorOccurred) and "get_task" in e.message for e in events)


async def test_a_crashing_agent_is_reported_and_its_work_still_verified() -> None:
    async def crashes(call: Call) -> TurnEndReason:
        await call("get_task")
        raise RuntimeError("claude exited with status 1")

    session, _, _ = session_for(crashes, steps=Steps(True))
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

    session, _, _ = session_for(gives_up, steps=Steps())
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
    session, _, _ = session_for(checks_first, steps=steps, limits=Limits(max_checks=1))
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

    session, _, _ = session_for(claims_twice, steps=steps)
    events = await asyncio.wait_for(run(session), 5)
    assert [t.status for t in board(events)] == ["completed"]


async def test_an_agent_that_lingers_after_finishing_is_stopped() -> None:
    async def lingers(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("complete_task", summary="done")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(lingers, exit_grace=0.01)
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

    session, _, launcher = session_for(waits, claims_done, steps=steps)
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
    session, host, launcher = session_for(claims_done, claims_done, steps=Steps(True, True))
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert host.starts == 1
    assert [a.id for a, *_ in launcher.launches] == ["agent-1", "agent-2"]
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
