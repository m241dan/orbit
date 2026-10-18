"""The startup check: the verification steps run once in the background on the base commit."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from orbit.agent import AgentSession, Setup, Verification, VerificationStep
from orbit.agent.application import session as session_module
from orbit.agent.application.startup_check import CANCELLED_NOTE, StartupCheck
from orbit.protocol import (
    GIT_FAILED,
    INTERNAL,
    PLANNER,
    UNKNOWN,
    AgentExited,
    AgentStarted,
    CancelAgent,
    ErrorOccurred,
    Event,
    Notice,
    SendMessage,
    StartupChecked,
    StepState,
    TaskStarted,
    TurnEndReason,
    VerificationChanged,
)
from tests.agent_fakes import (
    ACCEPTED,
    STEP,
    Call,
    FakeTree,
    Steps,
    agent_exited,
    board,
    events_until,
    payloads,
    refused,
    run,
    send,
    session_for,
)

LINT = VerificationStep(name="Lint", command=("ruff", "check"))
SNAPSHOT = "/work/review-HEAD"


def checked(steps: Steps) -> Verification:
    return Verification((LINT, STEP), steps)


def titled(*statuses: str) -> VerificationChanged:
    names = (LINT.name, STEP.name)
    return VerificationChanged(
        steps=tuple(StepState(name=n, status=s) for n, s in zip(names, statuses, strict=True)),  # type: ignore[arg-type]
        title="Startup check",
    )


async def until_checked(session: AgentSession) -> list[Event]:
    """The events until the startup check's agent exits."""
    return await events_until(session, agent_exited("startup-check"))


async def quiet(session: AgentSession) -> list[Event]:
    """The events published as the events start, and for a moment after."""
    events: list[Event] = []
    stream: AsyncIterator[Event] = payloads(session)
    try:
        while True:
            events.append(await asyncio.wait_for(anext(stream), 0.05))
    except TimeoutError:
        return events


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="done")
    return "completed"


async def test_the_steps_run_in_a_set_up_snapshot_of_the_base_and_pass() -> None:
    steps = Steps(True, True, True)
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)

    events = await until_checked(session)

    # Never in the coder's tree: in a snapshot of the base, set up first, then dropped.
    assert tree.snapshots == ["HEAD"] and tree.prepared == []
    assert steps.runs == ["setup", "Lint", "Unit tests"]
    assert steps.roots == [SNAPSHOT] * 3
    assert tree.dropped == [SNAPSHOT]
    startup = [e for e in events if not isinstance(e, VerificationChanged) or e.title]
    assert startup[-8:] == [
        AgentStarted(agent_id="startup-check", role="checker", task_id="startup"),
        titled("pending", "pending"),
        titled("running", "pending"),
        titled("passed", "pending"),
        titled("passed", "running"),
        titled("passed", "passed"),
        StartupChecked(passed=True),
        AgentExited(agent_id="startup-check", reason="finished"),
    ]
    await session.close()


async def test_a_failing_step_adds_a_draft_task_to_fix_it() -> None:
    steps = Steps(False)
    tree = FakeTree()
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)

    events = await until_checked(session)

    assert steps.runs == ["Lint"]
    assert titled("failed", "pending") in events
    assert StartupChecked(passed=False, step="Lint", output="1 test failed") in events
    [draft] = board(events)
    assert draft.status == "draft"
    assert draft.task.prompt.splitlines()[0] == "Fix failing check: Lint"
    assert "ruff check" in draft.task.prompt and "1 test failed" in draft.task.prompt
    assert draft.task.settings.tool_access == "full"  # The coder role's.
    assert AgentExited(agent_id="startup-check", reason="finished") in events
    await session.close()


async def test_a_failing_setup_is_the_failing_check() -> None:
    steps = Steps(False)
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)

    events = await until_checked(session)

    assert steps.runs == ["setup"]
    assert StartupChecked(passed=False, step="setup", output="1 test failed") in events
    assert board(events)[0].task.prompt.startswith("Fix failing check: setup\n")
    await session.close()


async def test_without_a_tree_it_runs_in_the_current_directory() -> None:
    steps = Steps(True, False)
    session = AgentSession(verification=checked(steps), check_at_startup=True)

    events = await until_checked(session)

    assert steps.roots == [".", "."]
    assert StartupChecked(passed=False, step="Unit tests", output="1 test failed") in events
    # Without agents, the draft has the default settings.
    assert board(events)[0].task.settings.tool_access == "restricted"
    await session.close()


async def test_it_is_skipped_without_verification_or_steps_or_when_not_asked() -> None:
    sessions = [
        AgentSession(check_at_startup=True),
        AgentSession(verification=Verification((), Steps()), check_at_startup=True),
        AgentSession(verification=checked(Steps())),
    ]
    for session in sessions:
        events = await quiet(session)
        assert not [e for e in events if isinstance(e, AgentStarted | StartupChecked)]
        await session.close()


def but_accepted(events: list[Event]) -> list[Event]:
    """``events`` without the outcomes of commands accepted."""
    return [event for event in events if event != ACCEPTED]


async def test_cancelling_its_agent_stops_it() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()
    tree = FakeTree()
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if event == titled("running", "pending"):
            await send(session, CancelAgent(agent_id="startup-check"))
        if isinstance(event, AgentExited):
            break

    assert ACCEPTED in events
    assert but_accepted(events)[-3:] == [
        titled("cancelled", "pending"),
        Notice(message=CANCELLED_NOTE),
        AgentExited(agent_id="startup-check", reason="killed"),
    ]
    assert tree.dropped == [SNAPSHOT]
    assert not [e for e in events if isinstance(e, StartupChecked)]
    # Once it's over, there's no agent by that name to cancel.
    await send(session, CancelAgent(agent_id="startup-check"))
    assert await anext(payloads(session)) == refused(UNKNOWN, "No running agent 'startup-check'.")
    await session.close()


async def test_cancelling_it_while_setting_up_leaves_the_steps_pending() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()
    tree = FakeTree()
    tree.setup = Setup(command=("uv", "sync"))
    session, _, _ = session_for(verification=checked(steps), tree=tree, check_at_startup=True)
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if event == titled("pending", "pending"):
            await send(session, CancelAgent(agent_id="startup-check"))
        if isinstance(event, AgentExited):
            break

    assert but_accepted(events)[-3:] == [
        titled("pending", "pending"),
        Notice(message=CANCELLED_NOTE),
        AgentExited(agent_id="startup-check", reason="killed"),
    ]
    await session.close()


async def test_closing_the_session_stops_it() -> None:
    steps = Steps(True)
    steps.gate = asyncio.Event()
    session, _, _ = session_for(verification=checked(steps), check_at_startup=True)
    async for event in payloads(session):
        if event == titled("running", "pending"):
            break
    await session.close()
    assert steps.runs == ["Lint"]


async def test_closing_before_it_starts_is_harmless() -> None:
    check = StartupCheck(checked(Steps()), None, asyncio.Queue[Event]().put, None)  # type: ignore[arg-type]
    await check.close()
    assert not check.running


async def test_a_snapshot_git_cannot_make_is_reported() -> None:
    class Broken(FakeTree):
        async def snapshot(self, commit: str) -> str:
            raise RuntimeError("git worktree failed: no commits")

    steps = Steps()
    session, _, _ = session_for(verification=checked(steps), tree=Broken(), check_at_startup=True)

    events = await until_checked(session)

    assert events[-2:] == [
        ErrorOccurred(
            code=GIT_FAILED,
            message="The startup check couldn't run: git worktree failed: no commits",
        ),
        AgentExited(agent_id="startup-check", reason="crashed"),
    ]
    assert steps.runs == []
    await session.close()


async def test_tasks_run_while_it_does_and_take_the_panel_over() -> None:
    gate = asyncio.Event()

    async def runner(step: VerificationStep, root: str) -> tuple[bool, str]:
        if root == SNAPSHOT:  # Only the startup check waits.
            await gate.wait()
        return True, "ok"

    tree = FakeTree()
    session, _, _ = session_for(
        claims_done,
        verification=Verification((LINT, STEP), runner),
        tree=tree,
        check_at_startup=True,
    )

    # The task runs to the end while the startup check waits on its first step.
    events = await run(session)
    assert board(events)[0].status == "completed"
    assert TaskStarted(task_id="1") in events

    gate.set()
    rest = await until_checked(session)
    assert StartupChecked(passed=True) in rest
    # The task's verification has the panel now; the startup check no longer reports to it.
    assert not [e for e in rest if isinstance(e, VerificationChanged) and e.title]
    await session.close()


async def says_nothing(call: Call) -> TurnEndReason:
    return "completed"


async def test_a_crash_is_reported_and_closing_still_stops_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(step: VerificationStep, output: str) -> str:
        raise ValueError("bug")

    monkeypatch.setattr(session_module, "startup_fix_prompt", broken)
    session, host, launcher = session_for(verification=checked(Steps(False)), check_at_startup=True)
    launcher.planner_turns.append(says_nothing)
    await send(session, SendMessage(agent=PLANNER, text="Hello."))  # Starts the host.
    events: list[Event] = []
    async for event in payloads(session):
        events.append(event)
        if isinstance(event, ErrorOccurred):
            break

    assert events[-1] == ErrorOccurred(
        code=INTERNAL,
        message="The startup check lane stopped on an unexpected error: ValueError('bug')",
    )
    assert AgentExited(agent_id="startup-check", reason="killed") in events
    await session.close()
    assert host.stops == 1
