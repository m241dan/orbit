"""Lanes from roles: a role in a lane of its own runs there, starting by itself beside the work
lane, which runs only on ctrl+r; a pause stops every lane, and each lane is busy and stopped by
name."""

import asyncio
from collections.abc import Callable

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.review_trace import REVIEW_SPAN
from orbit.agent.application.takeover import Takeover
from orbit.agent.domain.activity import LANE_TASK_RUNNING, reported
from orbit.agent.domain.endings import STOPPED_BY_YOU
from orbit.agent.domain.roles import Role
from orbit.agent.domain.usage_limits import WAIT_CANCELLED_NOTE
from orbit.protocol import (
    BUSY,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    UNKNOWN,
    AcceptTask,
    Activity,
    AddTask,
    BusyChanged,
    CancelAgent,
    CancelTurn,
    CommandRefused,
    EditTask,
    Event,
    FireTrigger,
    NewSession,
    Notice,
    OwnershipOffered,
    ReviewStatus,
    RunTasks,
    TakeOwnership,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
)
from tests.agent_fakes import (
    OWNER,
    ROLES,
    Call,
    FakeLock,
    FakeTree,
    Script,
    ScriptedLauncher,
    all_of,
    board,
    board_of,
    claims_done,
    events_until,
    greeted,
    idle,
    lane_of,
    of_type,
    refused,
    run,
    send,
    session_for,
    settle,
    settled,
    snapshot_of,
    suggests,
    turns_of,
)
from tests.lane_fakes import SECURITY, WITH_SECURITY, audit
from tests.telemetry_helpers import Traced

LIMIT = "You've hit your session limit · resets 6pm (UTC)"


class Held:
    """Agents' runs, each setting its ``started`` event once it has fetched its task, then
    waiting for its own gate before claiming its task done."""

    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}

    def script(self, name: str) -> Script:
        """The run named ``name``."""
        started = self.started.setdefault(name, asyncio.Event())
        gate = self.gates.setdefault(name, asyncio.Event())

        async def play(call: Call) -> TurnEndReason:
            await call("get_task")
            started.set()
            await gate.wait()
            await call("complete_task", summary="done")
            return "completed"

        return play

    async def until_started(self, name: str) -> None:
        """Wait until the run ``name`` has fetched its task."""
        await asyncio.wait_for(self.started[name].wait(), 5)

    def let(self, name: str) -> None:
        """Let the run ``name`` end."""
        self.gates[name].set()


def roles_of(launcher: ScriptedLauncher) -> list[str]:
    """The roles of the agents launched so far, in order."""
    return [launch.agent.role for launch in launcher.launches]


def statuses_by_role(events: list[Event]) -> list[tuple[str, str]]:
    """Each task's role and status, as the board was last published in ``events``."""
    return [(t.task.role, t.status) for t in board(events)]


def task_is(task_id: str, status: str) -> Callable[[Event], bool]:
    """Stops when task ``task_id`` is published with ``status``."""
    return lambda e: (
        isinstance(e, TasksChanged) and any(t.id == task_id and t.status == status for t in e.tasks)
    )


def state_of(session: WorkspaceHost, task_id: str) -> TaskState:
    return board_of(session).get(task_id)


async def test_a_task_in_a_lane_of_its_own_starts_at_once_beside_a_coder_task() -> None:
    held = Held()
    session, _, launcher = session_for(
        held.script("coder"), held.script("security"), roles=WITH_SECURITY, tree=FakeTree()
    )
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    await held.until_started("coder")
    await send(session, AddTask(task=audit()))
    await held.until_started("security")

    snapshot = await snapshot_of(session)
    assert snapshot.busy == {"work": True, "planner": False, "review": False, "security": True}
    assert [(la.agent.role, la.cwd) for la in launcher.launches] == [
        ("coder", "/work/coder"),
        ("security", "/work/review-base"),
    ]
    held.let("security")
    events = await events_until(session, idle("security"))
    assert statuses_by_role(events) == [("coder", "running"), ("security", "completed")]
    held.let("coder")
    await events_until(session, idle())
    await session.close()


async def test_a_coder_task_waits_for_ctrl_r_while_a_lane_of_its_own_runs_by_itself() -> None:
    session, _, launcher = session_for(claims_done, claims_done, roles=WITH_SECURITY)
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, AddTask(task=audit()))
    events = await events_until(session, idle("security"))
    assert roles_of(launcher) == ["security"]
    assert statuses_by_role(events) == [("coder", "pending"), ("security", "completed")]
    assert BusyChanged(busy=True, lane="security") in events

    await send(session, RunTasks())
    events = await events_until(session, idle())
    assert roles_of(launcher) == ["security", "coder"]
    assert [t.status for t in board(events)] == ["completed", "completed"]
    await session.close()


async def test_a_task_in_a_lane_of_its_own_runs_in_place_without_a_tree() -> None:
    session, _, launcher = session_for(claims_done, roles=WITH_SECURITY, root="/src/app")
    await send(session, AddTask(task=audit()))
    await events_until(session, idle("security"))
    [launch] = launcher.launches
    assert launch.cwd == "/src/app"
    await session.close()


class Paused:
    """A pause whose sleeps last until :meth:`release` is called."""

    def __init__(self) -> None:
        self.released = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        await self.released.wait()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(sleep=self.sleep)


async def errs(call: Call) -> TurnEndReason:
    return "error"


async def limited_session(paused: Paused) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose first coder hits the usage limit, so the work lane waits out a pause,
    then whose agents complete, with a security task queued during the pause."""
    session, _, launcher = session_for(
        errs, claims_done, claims_done, roles=WITH_SECURITY, pause=paused.pause
    )
    launcher.turn_errors.append(LIMIT)
    await run(session, TaskSpec(prompt="Code it."))
    assert turns_of(session).paused is not None
    await send(session, AddTask(task=audit()))
    await settled()
    return session, launcher


async def test_a_lane_of_its_own_starts_nothing_while_the_work_lane_waits_out_a_pause() -> None:
    paused = Paused()
    session, launcher = await limited_session(paused)
    assert roles_of(launcher) == ["coder"]
    assert state_of(session, "2").status == "pending"

    paused.released.set()
    await events_until(session, all_of(idle(), idle("security")))
    assert sorted(roles_of(launcher)) == ["coder", "coder", "security"]
    assert [t.status for t in board_of(session).tasks] == ["completed", "completed"]
    await session.close()


async def test_holding_the_work_lane_through_a_pause_lets_the_other_lanes_start() -> None:
    paused = Paused()
    session, launcher = await limited_session(paused)

    await send(session, CancelTurn())
    events = await events_until(session, idle("security"))
    assert Notice(message=WAIT_CANCELLED_NOTE) in events
    assert roles_of(launcher) == ["coder", "security"]
    assert [t.status for t in board_of(session).tasks] == ["error", "completed"]
    await session.close()


async def test_cancelling_a_lane_of_its_own_stops_only_its_task_and_it_carries_on() -> None:
    held = Held()
    session, _, launcher = session_for(
        held.script("coder"),
        held.script("audit-1"),
        held.script("audit-2"),
        roles=WITH_SECURITY,
    )
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    await held.until_started("coder")
    await send(session, AddTask(task=audit("First.")))
    await send(session, AddTask(task=audit("Second.")))
    await held.until_started("audit-1")

    await send(session, CancelTurn(lane="security"))
    await held.until_started("audit-2")
    first = state_of(session, "2")
    assert (first.status, first.reason) == ("cancelled", STOPPED_BY_YOU)
    assert state_of(session, "1").status == "running"
    held.let("audit-2")
    held.let("coder")
    events = await events_until(session, all_of(idle(), idle("security")))
    assert statuses_by_role(events) == [
        ("coder", "completed"),
        ("security", "cancelled"),
        ("security", "completed"),
    ]
    assert roles_of(launcher) == ["coder", "security", "security"]
    await session.close()


async def test_cancelling_an_agent_in_a_lane_of_its_own_stops_only_its_task() -> None:
    held = Held()
    session, _, launcher = session_for(
        held.script("coder"), held.script("security"), roles=WITH_SECURITY
    )
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    await held.until_started("coder")
    await send(session, AddTask(task=audit()))
    await held.until_started("security")
    auditor = launcher.launches[-1].agent.id

    await send(session, CancelAgent(agent_id=auditor))
    await events_until(session, idle("security"))
    assert state_of(session, "2").status == "cancelled"
    assert state_of(session, "1").status == "running"
    held.let("coder")
    await events_until(session, idle())
    await session.close()


async def test_a_task_in_a_lane_of_its_own_keeps_the_session_running_and_unswitchable() -> None:
    held = Held()
    session, _, _ = session_for(held.script("security"), roles=WITH_SECURITY)
    await send(session, AddTask(task=audit()))
    await held.until_started("security")
    assert reported(session.activity) == (Activity(kind="task_run", task_id="1"),)

    await send(session, NewSession())
    events = await events_until(session, of_type(CommandRefused))
    assert events[-1] == refused(BUSY, LANE_TASK_RUNNING)
    held.let("security")
    await events_until(session, idle("security"))
    assert not session.activity.running
    await session.close()


async def test_accepting_a_draft_in_a_lane_of_its_own_starts_it() -> None:
    session, _, launcher = session_for(claims_done, roles=WITH_SECURITY)
    board_of(session).add_draft(audit())
    await send(session, AcceptTask(task_id="1"))
    await events_until(session, idle("security"))
    assert roles_of(launcher) == ["security"]
    await session.close()


async def test_editing_a_task_into_a_lane_of_its_own_starts_it() -> None:
    session, _, launcher = session_for(claims_done, roles=WITH_SECURITY)
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, EditTask(task_id="1", task=audit()))
    events = await events_until(session, idle("security"))
    assert roles_of(launcher) == ["security"]
    assert statuses_by_role(events) == [("security", "completed")]
    await session.close()


async def test_running_tasks_runs_what_waits_in_the_lanes_of_their_own() -> None:
    held = Held()
    session, _, launcher = session_for(held.script("first"), claims_done, roles=WITH_SECURITY)
    await send(session, AddTask(task=audit()))
    await held.until_started("first")
    await send(session, CancelTurn(lane="security"))
    await events_until(session, task_is("1", "cancelled"))
    await events_until(session, idle("security"))

    await send(session, RunTasks())
    await events_until(session, idle("security"))
    assert roles_of(launcher) == ["security", "security"]
    assert state_of(session, "1").status == "completed"
    await session.close()


BUILD_ROLES = {
    **ROLES,
    "coder": ROLES["coder"].model_copy(update={"lane": "build"}),
    "configurator": ROLES["configurator"].model_copy(update={"lane": "build"}),
}
"""The built-in roles, with the coder and configurator in a lane named ``build``."""


async def test_the_merging_lane_is_named_by_its_roles() -> None:
    held = Held()
    session, _, _ = session_for(held.script("coder"), roles=BUILD_ROLES)
    assert (await snapshot_of(session)).busy == {"build": False, "planner": False, "review": False}
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    events = await events_until(session, of_type(BusyChanged))
    assert events[-1] == BusyChanged(busy=True, lane="build")
    await held.until_started("coder")

    await send(session, CancelTurn(lane="work"))
    events = await events_until(session, of_type(CommandRefused))
    message = "No lane 'work'; the lanes are build, planner and review."
    assert events[-1] == refused(UNKNOWN, message)
    await send(session, CancelTurn(lane="build"))
    await events_until(session, idle("build"))
    assert state_of(session, "1").status == "cancelled"
    await session.close()


def with_review_roles(
    reviewer: dict[str, object], super_reviewer: dict[str, object]
) -> dict[str, Role]:
    """The built-in roles, with the ``reviewer`` and ``super_reviewer`` fields changed."""
    return {
        **ROLES,
        REVIEWER: ROLES[REVIEWER].model_copy(update=reviewer),
        SUPER_REVIEWER: ROLES[SUPER_REVIEWER].model_copy(update=super_reviewer),
    }


async def test_reviews_are_off_while_the_reviewer_shares_the_merging_lane() -> None:
    roles = with_review_roles({"lane": "work"}, {"lane": "work"})
    session, _, _ = session_for(roles=roles, tree=FakeTree(), review=True)
    assert session._lane is None
    assert (await snapshot_of(session)).busy == {"work": False, "planner": False}
    await session.close()


async def test_reviews_are_off_while_the_super_reviewer_alone_is_in_the_merging_lane() -> None:
    roles = with_review_roles({"enabled": False}, {"lane": "work"})
    session, _, _ = session_for(roles=roles, tree=FakeTree(), review=True)
    assert session._lane is None
    await session.close()


async def test_with_the_reviewer_disabled_super_reviews_run_in_the_super_reviewers_lane() -> None:
    roles = with_review_roles({"enabled": False}, {"lane": "audit"})
    session, _, _ = session_for(reviewers=(suggests(),), tree=FakeTree(), review=True, roles=roles)
    lane = lane_of(session)
    assert (lane.runner.lane, lane.roles) == ("audit", frozenset({SUPER_REVIEWER}))
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    assert [t.task.role for t in board(events)] == [SUPER_REVIEWER]
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


WITH_AUDITOR = {**ROLES, "auditor": SECURITY.model_copy(update={"lane": "review"})}
"""The built-in roles, with ``auditor`` in the review lane, working in a snapshot."""

AUDIT = TaskSpec(prompt="Audit it.", role="auditor")
"""A task for the ``auditor`` role."""


async def test_the_review_lane_runs_the_other_roles_in_its_lane() -> None:
    session, _, launcher = session_for(
        claims_done, roles=WITH_AUDITOR, tree=FakeTree(), review=True
    )
    assert lane_of(session).roles == frozenset({"reviewer", "super-reviewer", "auditor"})
    await send(session, AddTask(task=AUDIT))
    await events_until(session, idle("review"))
    assert [(la.agent.role, la.cwd) for la in launcher.launches] == [
        ("auditor", "/work/review-base")
    ]
    assert state_of(session, "1").status == "completed"
    await session.close()


async def test_another_roles_task_in_the_review_lane_is_reported_as_a_task_run() -> None:
    held = Held()
    session, _, _ = session_for(
        held.script("auditor"), roles=WITH_AUDITOR, tree=FakeTree(), review=True
    )
    await send(session, AddTask(task=AUDIT))
    await held.until_started("auditor")
    assert reported(session.activity) == (Activity(kind="task_run", task_id="1"),)

    await send(session, NewSession())
    events = await events_until(session, of_type(CommandRefused))
    assert events[-1] == refused(BUSY, LANE_TASK_RUNNING)
    held.let("auditor")
    await events_until(session, idle("review"))
    assert not session.activity.running
    await session.close()


async def test_stopping_another_roles_task_in_the_review_lane_stops_it_as_a_task() -> None:
    traced = Traced()
    held = Held()
    session, _, _ = session_for(
        held.script("auditor"),
        roles=WITH_AUDITOR,
        tree=FakeTree(),
        review=True,
        telemetry=traced.telemetry,
    )
    await send(session, AddTask(task=AUDIT))
    await held.until_started("auditor")

    await send(session, CancelTurn(lane="review"))
    await events_until(session, idle("review"))
    stopped = state_of(session, "1")
    assert (stopped.status, stopped.reason) == ("cancelled", STOPPED_BY_YOU)
    await session.close()
    assert traced.spans(REVIEW_SPAN) == []


async def test_a_takeover_ending_as_the_session_closes_starts_no_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closing, let_close = asyncio.Event(), asyncio.Event()
    running_queued, let_run = asyncio.Event(), asyncio.Event()
    close_takeover = Takeover.close

    async def held_close(takeover: Takeover) -> None:
        closing.set()
        await let_close.wait()
        await close_takeover(takeover)

    async def held_run() -> None:
        running_queued.set()
        await let_run.wait()

    monkeypatch.setattr(Takeover, "close", held_close)
    lock = FakeLock(free=True)
    session, _, launcher = session_for(claims_done, owner=OWNER, lock=lock, roles=WITH_SECURITY)
    await greeted(session)
    await events_until(session, lambda event: isinstance(event, OwnershipOffered))
    await send(session, AddTask(task=audit()))
    await send(session, RunTasks())
    turns_of(session).run_tasks = held_run  # type: ignore[method-assign]
    taking = asyncio.ensure_future(send(session, TakeOwnership(take=True)))
    await asyncio.wait_for(running_queued.wait(), 5)
    stopping = asyncio.ensure_future(session.close())
    await asyncio.wait_for(closing.wait(), 5)

    let_run.set()
    await asyncio.wait_for(taking, 5)
    await settled()
    assert launcher.launches == [] and state_of(session, "1").status == "pending"
    let_close.set()
    await asyncio.wait_for(stopping, 5)
