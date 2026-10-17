"""How a session runs its task list, now that every task runs through an agent."""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from orbit.agent import (
    NO_STEPS_NOTE,
    AgentSession,
    Verification,
    VerificationStep,
    run_step,
)
from orbit.agent.application.agent_run import AgentRun
from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.board import TaskBoard
from orbit.protocol import (
    AddTask,
    AgentExited,
    BusyChanged,
    CancelTurn,
    EditTask,
    ErrorOccurred,
    Event,
    Notice,
    RemoveTask,
    RunTasks,
    Settings,
    StepState,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    TurnEndReason,
    TurnFinished,
    Usage,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeHost,
    FakeTree,
    FakeWorkspace,
    ScriptedLauncher,
    Steps,
    board,
    nothing_queued,
    run,
    session_for,
    verified,
)


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="done")
    return "completed"


async def never_starts(call: Call) -> TurnEndReason:
    return "completed"


async def keeps_claiming(call: Call) -> TurnEndReason:
    await call("get_task")
    while "another agent" not in (await call("complete_task", summary="try")).text:
        pass
    return "completed"


async def idle(session: AgentSession) -> list[Event]:
    """The session's events until it next goes idle."""
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if event == BusyChanged(busy=False):
            return events
    raise AssertionError("unreachable")


def statuses(events: list[Event]) -> list[str]:
    return [t.status for t in board(events)]


async def test_each_task_gets_its_own_agent_with_its_settings() -> None:
    session, _, launcher = session_for(claims_done, claims_done)
    first = TaskSpec(prompt="one", settings=Settings(model="opus", tool_access="full"))
    second = TaskSpec(prompt="two", settings=Settings(tool_access="off"))
    events = await asyncio.wait_for(run(session, first, second), 5)

    assert [
        (launch.agent.id, launch.agent.task_id, launch.settings) for launch in launcher.launches
    ] == [
        ("agent-1", "1", first.settings),
        ("agent-2", "2", second.settings),
    ]
    running = TasksChanged(
        tasks=(TaskState(id="1", task=first, status="running"), TaskState(id="2", task=second))
    )
    assert events.index(running) < events.index(TaskStarted(task_id="1"))
    assert statuses(events) == ["completed", "completed"]
    assert session.settings == Settings()


async def test_running_again_runs_only_tasks_that_did_not_finish() -> None:
    session, _, launcher = session_for(claims_done, never_starts, claims_done)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert statuses(events) == ["completed", "error"]

    await session.send(RunTasks())
    assert statuses(await asyncio.wait_for(idle(session), 5)) == ["completed", "completed"]
    assert [launch.agent.task_id for launch in launcher.launches] == ["1", "2", "2"]


async def test_running_does_nothing_when_nothing_is_runnable() -> None:
    session, _, launcher = session_for()
    stream = session.events()
    await anext(stream)  # Settings.
    await session.send(RunTasks())
    await asyncio.sleep(0.01)
    assert nothing_queued(session)
    assert launcher.launches == []


async def test_running_without_agents_is_refused() -> None:
    session = AgentSession()
    await session.send(AddTask(task=TaskSpec(prompt="a")))
    await session.send(RunTasks())
    stream = session.events()
    events = [await anext(stream) for _ in range(3)]
    assert ErrorOccurred(message="Tasks need agents, and none are set up.") in events


async def test_a_failed_task_does_not_stop_the_next() -> None:
    session, _, _ = session_for(never_starts, claims_done)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert statuses(events) == ["error", "completed"]


async def test_each_task_adds_up_the_usage_of_its_agents_turns() -> None:
    session, _, launcher = session_for(claims_done, claims_done)
    one, two, three = Usage(input_tokens=10, cost_usd=0.5), Usage(output_tokens=5), Usage(turns=1)
    launcher.usages = [(one, two), (three,)]
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert [t.usage for t in board(events)] == [one + two, three]
    # The turns' own usage still reaches the frontend, which keeps the session's total.
    turns = [e.usage for e in events if isinstance(e, TurnFinished)]
    assert turns == [one, two, three]


async def test_running_is_refused_while_a_run_runs() -> None:
    gate = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="ok")
        return "completed"

    session, _, launcher = session_for(waits, claims_done)
    await session.send(AddTask(task=TaskSpec(prompt="a")))
    await session.send(AddTask(task=TaskSpec(prompt="b")))
    await session.send(RunTasks())
    await session.send(RunTasks())
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if isinstance(event, ErrorOccurred):
            break
    assert events[-1] == ErrorOccurred(message="Tasks are already running.")
    gate.set()
    assert statuses(await asyncio.wait_for(idle(session), 5)) == ["completed", "completed"]
    assert len(launcher.launches) == 2


async def test_tasks_added_during_a_run_join_it_and_running_ones_cannot_be_edited() -> None:
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await proceed.wait()
        await call("complete_task", summary="done")
        return "completed"

    session, _, launcher = session_for(waits, claims_done)
    await session.send(AddTask(task=TaskSpec(prompt="one")))
    await session.send(RunTasks())
    await asyncio.wait_for(started.wait(), 5)
    await session.send(EditTask(task_id="1", task=TaskSpec(prompt="changed")))
    await session.send(AddTask(task=TaskSpec(prompt="two")))
    proceed.set()
    events = await asyncio.wait_for(idle(session), 5)

    assert ErrorOccurred(message="Task '1' can't be edited while it runs.") in events
    assert [launch.agent.task_id for launch in launcher.launches] == ["1", "2"]
    assert statuses(events) == ["completed", "completed"]


async def test_verification_progress_is_reported_step_by_step() -> None:
    session, _, _ = session_for(claims_done, verification=verified(Steps(True)))
    await session.send(AddTask(task=TaskSpec(prompt="one")))
    await session.send(RunTasks())
    events = await asyncio.wait_for(idle(session), 5)

    def steps(status: str) -> VerificationChanged:
        return VerificationChanged(steps=(StepState(name="Unit tests", status=status),))  # type: ignore[arg-type]

    def task(status: str) -> TasksChanged:
        state = TaskState(id="1", task=TaskSpec(prompt="one"), status=status)  # type: ignore[arg-type]
        return TasksChanged(tasks=(state,))

    run_events = events[events.index(BusyChanged(busy=True)) :]
    shown = [
        e
        for e in run_events
        if isinstance(
            e,
            TasksChanged
            | VerificationChanged
            | TaskStarted
            | VerificationStarted
            | VerificationFinished,
        )
    ]
    assert shown == [
        task("running"),
        steps("pending"),
        TaskStarted(task_id="1"),
        task("verifying"),
        steps("running"),
        VerificationStarted(task_id="1", step=0),
        steps("passed"),
        VerificationFinished(task_id="1", step=0, passed=True),
        task("completed"),
    ]


async def test_a_fallback_fix_runs_next_with_the_failed_tasks_settings() -> None:
    settings = Settings(model="opus", tool_access="full")
    session, _, launcher = session_for(
        keeps_claiming,
        claims_done,
        claims_done,
        verification=verified(Steps(False, False, True, True)),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(
        run(session, TaskSpec(prompt="one", settings=settings), TaskSpec(prompt="two")), 5
    )
    # keeps_claiming is asked for a fix it doesn't make, so its work falls back.
    one, fix, two = board(events)
    assert (fix.fixes, fix.task.settings) == (one.id, settings)
    assert [launch.agent.task_id for launch in launcher.launches] == [one.id, fix.id, two.id]
    assert statuses(events) == ["completed", "completed", "completed"]


async def test_a_fix_completes_after_the_task_it_fixed_is_removed() -> None:
    sessions: list[AgentSession] = []

    async def removes_what_it_fixes(call: Call) -> TurnEndReason:
        await call("get_task")
        await sessions[0].send(RemoveTask(task_id="1"))
        await call("complete_task", summary="fixed")
        return "completed"

    session, _, launcher = session_for(
        keeps_claiming,
        removes_what_it_fixes,
        claims_done,
        verification=verified(Steps(False, False, True, True)),
        limits=Limits(max_attempts=1),
    )
    sessions.append(session)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="one"), TaskSpec(prompt="two")), 5)
    assert not [e for e in events if isinstance(e, ErrorOccurred)]
    assert [launch.agent.task_id for launch in launcher.launches] == ["1", "3", "2"]
    assert [(t.id, t.status) for t in board(events)] == [("3", "completed"), ("2", "completed")]


def file_step(path: Path, name: str) -> VerificationStep:
    code = f"import os, sys; sys.exit(0 if os.path.exists({str(path)!r}) else 1)"
    return VerificationStep(name=name, command=(sys.executable, "-c", code))


def step_fixer(root: Path) -> Callable[[Call], Awaitable[TurnEndReason]]:
    """An agent that fixes whichever step orbit says failed by creating its file."""
    prefix = "Fix the failed verification step: "

    async def fixer(call: Call) -> TurnEndReason:
        await call("get_task")
        reply = (await call("complete_task", summary="done")).text
        while reply.startswith(prefix):
            (root / reply.split("\n", 1)[0].removeprefix(prefix)).touch()
            reply = (await call("complete_task", summary="fixed")).text
        return "completed"

    return fixer


async def test_one_agent_fixes_failing_steps_one_after_another(tmp_path: Path) -> None:
    steps = tuple(file_step(tmp_path / name, name) for name in ("lint", "types", "tests"))
    session, _, launcher = session_for(
        step_fixer(tmp_path), verification=Verification(steps, run_step)
    )
    events = await asyncio.wait_for(run(session), 10)

    assert len(launcher.launches) == 1
    failed = [e.step for e in events if isinstance(e, VerificationFinished) and not e.passed]
    assert failed == [0, 1, 2]
    assert statuses(events) == ["completed"]
    assert board(events)[0].attempt == 3


async def test_verification_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    steps = (file_step(tmp_path / "missing", "first"), file_step(tmp_path, "second"))
    session, _, _ = session_for(
        keeps_claiming,
        verification=Verification(steps, run_step, fallback_fixes=0),
        limits=Limits(max_attempts=1),
    )
    events = await asyncio.wait_for(run(session), 10)
    assert {e.step for e in events if isinstance(e, VerificationFinished)} == {0}


async def test_the_fix_cap_survives_a_cancelled_and_restarted_run() -> None:
    fetched = asyncio.Event()

    async def waits_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        fetched.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, launcher = session_for(
        keeps_claiming,
        waits_forever,
        keeps_claiming,
        verification=verified(Steps(False, False, False, False), fallback_fixes=1),
        limits=Limits(max_attempts=1),
    )
    await session.send(AddTask(task=TaskSpec(prompt="a")))
    await session.send(RunTasks())
    await asyncio.wait_for(fetched.wait(), 5)  # The fix is running.
    await session.send(CancelTurn())
    first = await asyncio.wait_for(idle(session), 5)
    assert statuses(first) == ["unverified", "cancelled"]

    await session.send(RunTasks())
    second = await asyncio.wait_for(idle(session), 5)

    assert len(launcher.launches) == 3
    assert statuses(second) == ["unverified", "unverified"]
    errors = [e.message for e in second if isinstance(e, ErrorOccurred)]
    assert errors == ["Verification step 'Unit tests' still fails after 1 fix attempts."]


async def test_tasks_with_tools_off_skip_verification() -> None:
    workspace = FakeWorkspace()
    steps = Steps()
    session, _, _ = session_for(claims_done, verification=verified(steps, workspace))
    events = await asyncio.wait_for(
        run(session, TaskSpec(prompt="a", settings=Settings(tool_access="off"))), 5
    )
    assert VerificationSkipped(task_id="1", reason="tools_off") in events
    assert steps.runs == [] and workspace.states == []
    assert statuses(events) == ["completed"]


async def test_tasks_that_change_no_files_skip_verification() -> None:
    steps = Steps()
    session, _, _ = session_for(claims_done, verification=verified(steps, FakeWorkspace("t", "t")))
    events = await asyncio.wait_for(run(session), 5)
    assert VerificationSkipped(task_id="1", reason="no_changes") in events
    assert steps.runs == []
    assert statuses(events) == ["completed"]


async def test_without_steps_tasks_skip_verification_and_say_so() -> None:
    workspace = FakeWorkspace("a", "b")
    session, _, _ = session_for(claims_done, verification=Verification((), Steps(), workspace))
    events = await asyncio.wait_for(run(session), 5)
    assert events[0] == Notice(message=NO_STEPS_NOTE)
    assert [e for e in events if isinstance(e, Notice)] == [Notice(message=NO_STEPS_NOTE)]
    assert VerificationSkipped(task_id="1", reason="no_steps") in events
    assert statuses(events) == ["completed"]


async def test_without_steps_unchanged_tasks_skip_for_that_reason() -> None:
    workspace = FakeWorkspace("a", "a")
    session, _, _ = session_for(claims_done, verification=Verification((), Steps(), workspace))
    events = await asyncio.wait_for(run(session), 5)
    assert VerificationSkipped(task_id="1", reason="no_changes") in events


async def test_with_verification_off_there_is_no_note() -> None:
    session, _, _ = session_for(claims_done)
    events = await asyncio.wait_for(run(session), 5)
    assert not any(isinstance(e, Notice | VerificationSkipped) for e in events)


async def test_tasks_are_verified_when_the_tree_cannot_be_read() -> None:
    steps = Steps(True)
    session, _, _ = session_for(
        claims_done, verification=verified(steps, FakeWorkspace(None, None))
    )
    events = await asyncio.wait_for(run(session), 5)
    assert steps.runs == ["Unit tests"]
    assert statuses(events) == ["completed"]


async def test_usage_arriving_after_the_task_was_removed_is_skipped() -> None:
    tasks = TaskBoard()
    task_id = tasks.add(TaskSpec(prompt="a"))

    async def claims_done_then_removed(call: Call) -> TurnEndReason:
        reason = await claims_done(call)
        # The task is completed now, so the user may remove it before the usage arrives.
        tasks.set_status(task_id, "completed")
        tasks.remove(task_id)
        return reason

    host = FakeHost()
    launcher = ScriptedLauncher(host, claims_done_then_removed)
    launcher.usages = [(Usage(input_tokens=10),)]
    events: list[Event] = []

    async def publish(event: Event) -> None:
        events.append(event)

    desk = AgentDesk(Agents(host, launcher, lambda: "token-1", ROLES, exit_grace=0.5), publish)
    outcome = await asyncio.wait_for(
        AgentRun(task_id, Tasks(tasks, publish), None, desk).run(None, "."), 5
    )
    assert outcome.status == "completed"
    assert AgentExited(agent_id="agent-1", reason="finished") in events
    assert not [e for e in events if isinstance(e, ErrorOccurred | TasksChanged)]


async def test_an_unexpected_error_in_the_work_lane_is_reported() -> None:
    tree = FakeTree()
    tree.failures["show"] = ValueError("the lane tripped")
    session, _, launcher = session_for(claims_done, claims_done, tree=tree, review=True)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    message = "The work lane stopped on an unexpected error: ValueError('the lane tripped')"
    assert ErrorOccurred(message=message) in events
    assert len(launcher.launches) == 1


async def test_a_task_whose_lane_crashes_ends_in_error() -> None:
    tree = FakeTree()
    tree.failures["prepare"] = ValueError("boom")
    session, _, launcher = session_for(claims_done, tree=tree)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a")), 5)
    message = "The work lane stopped on an unexpected error: ValueError('boom')"
    assert ErrorOccurred(message=message) in events
    assert statuses(events) == ["error"] and launcher.launches == []


async def test_cancelling_a_run_before_it_starts_is_no_error() -> None:
    session, _, launcher = session_for(claims_done)
    await session.send(AddTask(task=TaskSpec(prompt="a")))
    await session.send(RunTasks())
    await session.send(CancelTurn())  # The run's task hasn't had a chance to start yet.
    events = await asyncio.wait_for(idle(session), 5)
    assert not any(isinstance(e, ErrorOccurred) for e in events)
    assert statuses(events) == ["pending"] and launcher.launches == []
