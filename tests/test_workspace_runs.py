"""Each task runs in its role's workspace: a role working in none in an empty temporary directory
gone once its task ends, however it does; one in the checkout in the workspace's root; one on a
snapshot in a fresh snapshot of the base, dropped after; the built-in roles where they always
have. The planner keeps one such directory for its whole conversation, gone once it ends."""

import asyncio
from typing import Any

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.ports import Publish, ResumeFailed, TempDirFailed, TreeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import Role, Workspace
from orbit.protocol import (
    AGENT_FAILED,
    GIT_FAILED,
    PLANNER,
    PLANNER_LANE,
    REVIEWER,
    AddTask,
    AgentExited,
    CancelTurn,
    ConversationReset,
    ErrorOccurred,
    Event,
    ResetConversation,
    RunTasks,
    SendMessage,
    Settings,
    TaskSpec,
    TurnEndReason,
    TurnFinished,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeTempDirs,
    FakeTree,
    Script,
    ScriptedLauncher,
    agents_for,
    board,
    board_of,
    chats,
    claims_done,
    events_until,
    hangs,
    idle,
    of_type,
    plan,
    send,
    session_for,
    sole,
    until_idle,
)
from tests.waiting import eventually


def sketcher(workspace: Workspace, lane: str = "sketch") -> dict[str, Role]:
    """The built-in roles, with ``sketch`` working in ``workspace`` in ``lane``."""
    sketch = Role(instructions="Sketch it.", verify=False, lane=lane, workspace=workspace)
    return {**ROLES, "sketch": sketch}


def sketch(prompt: str = "Sketch it.") -> TaskSpec:
    """A task for the ``sketch`` role."""
    return TaskSpec(prompt=prompt, role="sketch")


async def test_a_role_working_in_none_runs_in_a_temporary_directory_gone_after_its_task() -> None:
    temp_dirs, tree = FakeTempDirs(), FakeTree()
    session, _, launcher = session_for(
        claims_done, roles=sketcher("none"), tree=tree, temp_dirs=temp_dirs
    )
    await send(session, AddTask(task=sketch()))
    events = await events_until(session, idle("sketch"))
    assert [launch.cwd for launch in launcher.launches] == ["/tmp/orbit-1"]
    assert temp_dirs.removed == ["/tmp/orbit-1"]
    assert (tree.prepared, tree.snapshots) == ([], [])
    assert [task.status for task in board(events)] == ["completed"]
    await session.close()


async def test_a_role_working_in_none_leaves_no_temporary_directory_when_cancelled() -> None:
    started = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    temp_dirs = FakeTempDirs()
    session, _, _ = session_for(endless, roles=sketcher("none"), temp_dirs=temp_dirs)
    await send(session, AddTask(task=sketch()))
    await asyncio.wait_for(started.wait(), 5)
    assert (temp_dirs.made, temp_dirs.removed) == (["/tmp/orbit-1"], [])

    await send(session, CancelTurn(lane="sketch"))
    await events_until(session, idle("sketch"))
    assert temp_dirs.removed == ["/tmp/orbit-1"]
    assert board_of(session).get("1").status == "cancelled"
    await session.close()


async def test_a_task_whose_temporary_directory_cant_be_made_ends_in_error() -> None:
    temp_dirs = FakeTempDirs()
    message = "Couldn't make a temporary directory: disk full"
    temp_dirs.failure = TempDirFailed(message)
    session, _, launcher = session_for(roles=sketcher("none"), temp_dirs=temp_dirs)
    await send(session, AddTask(task=sketch()))
    events = await events_until(session, idle("sketch"))
    assert launcher.launches == []
    assert ErrorOccurred(code=AGENT_FAILED, message=message) in events
    [task] = board(events)
    assert (task.status, task.reason_detail) == ("error", message)
    await session.close()


async def test_a_role_working_in_the_checkout_runs_in_the_workspaces_root() -> None:
    temp_dirs, tree = FakeTempDirs(), FakeTree()
    session, _, launcher = session_for(
        claims_done, roles=sketcher("checkout"), tree=tree, root="/src/app", temp_dirs=temp_dirs
    )
    await send(session, AddTask(task=sketch()))
    await events_until(session, idle("sketch"))
    assert [launch.cwd for launch in launcher.launches] == ["/src/app"]
    assert (tree.prepared, tree.snapshots, temp_dirs.made) == ([], [], [])
    await session.close()


async def test_a_role_on_a_snapshot_gets_a_fresh_one_of_the_base_dropped_after_its_run() -> None:
    tree = FakeTree()
    session, _, launcher = session_for(
        claims_done, claims_done, roles=sketcher("snapshot"), tree=tree
    )
    for prompt in ("First.", "Second."):
        await send(session, AddTask(task=sketch(prompt)))
        await events_until(session, idle("sketch"))
    assert [launch.cwd for launch in launcher.launches] == ["/work/review-base"] * 2
    assert (tree.snapshots, tree.dropped) == (["base"] * 2, ["/work/review-base"] * 2)
    assert tree.prepared == []
    await session.close()


async def test_a_role_working_in_none_in_the_work_lane_runs_there_and_lands_nothing() -> None:
    temp_dirs, tree = FakeTempDirs(), FakeTree()
    session, _, launcher = session_for(
        claims_done, claims_done, roles=sketcher("none", "work"), tree=tree, temp_dirs=temp_dirs
    )
    await send(session, AddTask(task=sketch()))
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    events = await events_until(session, idle())
    assert [(la.agent.role, la.cwd) for la in launcher.launches] == [
        ("sketch", "/tmp/orbit-1"),
        ("coder", "/work/coder"),
    ]
    assert temp_dirs.removed == ["/tmp/orbit-1"]
    assert tree.prepared == [False]  # Only for the coder's task, whose work alone lands.
    assert [task.status for task in board(events)] == ["completed", "completed"]
    assert len(tree.commits) == 1
    await session.close()


async def test_coder_tasks_run_in_the_coders_tree_and_the_planner_in_the_checkout() -> None:
    temp_dirs, tree = FakeTempDirs(), FakeTree()
    session, _, launcher = session_for(claims_done, tree=tree, root="/src/app", temp_dirs=temp_dirs)
    launcher.planner_turns = [chats]
    await plan(session, "Plan it.")
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    await events_until(session, idle())
    assert [(la.agent.role, la.cwd) for la in launcher.launches] == [
        (PLANNER, "/src/app"),
        ("coder", "/work/coder"),
    ]
    assert (tree.snapshots, temp_dirs.made) == ([], [])
    await session.close()


def planning_in(workspace: Workspace) -> dict[str, Role]:
    """The built-in roles, with the planner working in ``workspace``."""
    return {**ROLES, PLANNER: ROLES[PLANNER].model_copy(update={"workspace": workspace})}


def cwds(launcher: ScriptedLauncher) -> list[str]:
    """Where ``launcher``'s agents ran, a launch at a time."""
    return [launch.cwd for launch in launcher.launches]


async def reset(session: WorkspaceHost) -> None:
    """Reset the planner's conversation, waiting until it's reset."""
    await send(session, ResetConversation(agent=PLANNER))
    await events_until(session, of_type(ConversationReset))


async def fails(call: Call) -> TurnEndReason:
    raise RuntimeError("Could not start 'claude'.")


async def resume_lost(call: Call) -> TurnEndReason:
    raise ResumeFailed("No conversation found with session ID: saved-1")


async def unheard(event: Event) -> None:
    """Publish nothing."""


def chat_in_none(
    temp_dirs: FakeTempDirs, *turns: Script, publish: Publish = unheard
) -> tuple[PlannerChat, ScriptedLauncher]:
    """A chat with a planner working in none, in ``temp_dirs``, playing ``turns`` and publishing
    with ``publish``, and the launcher that plays them."""
    agents, launcher = agents_for(roles=planning_in("none"), temp_dirs=temp_dirs)
    launcher.planner_turns = list(turns)
    desk = AgentDesk(agents, publish)
    return PlannerChat(desk, Tasks(TaskBoard(), publish), lambda: ()), launcher


async def test_a_planner_working_in_none_keeps_one_temporary_directory_until_closed() -> None:
    temp_dirs = FakeTempDirs()
    session, _, launcher = session_for(roles=planning_in("none"), temp_dirs=temp_dirs)
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan it.")
    await plan(session, "And then?")
    assert cwds(launcher) == ["/tmp/orbit-1"] * 2
    assert (temp_dirs.made, temp_dirs.removed) == (["/tmp/orbit-1"], [])
    await session.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def test_a_reset_removes_the_planners_temporary_directory_and_the_next_has_its_own() -> None:
    temp_dirs = FakeTempDirs()
    session, _, launcher = session_for(roles=planning_in("none"), temp_dirs=temp_dirs)
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan it.")
    await reset(session)
    assert temp_dirs.removed == ["/tmp/orbit-1"]
    await plan(session, "Plan again.")
    assert cwds(launcher) == ["/tmp/orbit-1", "/tmp/orbit-2"]
    await session.close()
    assert temp_dirs.removed == ["/tmp/orbit-1", "/tmp/orbit-2"]


async def test_a_reset_reads_the_planners_workspace_again() -> None:
    temp_dirs = FakeTempDirs()
    in_none = planning_in("none")[PLANNER]
    session, _, launcher = session_for(
        root="/src/app", temp_dirs=temp_dirs, planner_role=lambda: in_none
    )
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan it.")
    await reset(session)
    await plan(session, "Plan again.")
    assert cwds(launcher) == ["/src/app", "/tmp/orbit-1"]
    await session.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def test_a_planner_on_a_snapshot_keeps_one_snapshot_until_closed() -> None:
    tree = FakeTree()
    session, _, launcher = session_for(roles=planning_in("snapshot"), tree=tree)
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan it.")
    await plan(session, "And then?")
    assert cwds(launcher) == ["/work/review-base"] * 2
    assert (tree.snapshots, tree.dropped) == (["base"], [])
    await session.close()
    assert tree.dropped == ["/work/review-base"]


async def test_a_planners_snapshot_left_over_at_close_is_reported_not_raised() -> None:
    tree = FakeTree()
    tree.failures["drop"] = TreeFailed("worktree locked")
    session, _, launcher = session_for(roles=planning_in("snapshot"), tree=tree)
    launcher.planner_turns = [chats]
    await plan(session, "Plan it.")
    await session.close()
    events = await events_until(session, of_type(ErrorOccurred))
    left = "The snapshot at /work/review-base couldn't be removed and is left over"
    assert events[-1] == ErrorOccurred(code=GIT_FAILED, message=f"{left}: worktree locked")


async def test_a_cancelled_planner_turn_leaves_its_directory_to_the_next_until_closed() -> None:
    temp_dirs = FakeTempDirs()
    session, _, launcher = session_for(roles=planning_in("none"), temp_dirs=temp_dirs)
    launcher.planner_turns = [hangs, chats]
    await send(session, SendMessage(agent=PLANNER, text="Plan it."))
    await eventually(lambda: launcher.launches)
    await send(session, CancelTurn(lane=PLANNER_LANE))
    await until_idle(session, PLANNER_LANE)
    assert temp_dirs.removed == []
    await plan(session, "Again.")
    assert cwds(launcher) == ["/tmp/orbit-1"] * 2
    await session.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def test_a_failed_planner_turn_leaves_its_directory_to_the_next_until_closed() -> None:
    temp_dirs = FakeTempDirs()
    session, _, launcher = session_for(roles=planning_in("none"), temp_dirs=temp_dirs)
    launcher.planner_turns = [fails, chats]
    assert sole(await plan(session, "Plan it."), TurnFinished).reason == "error"
    assert temp_dirs.removed == []
    await plan(session, "Again.")
    assert cwds(launcher) == ["/tmp/orbit-1"] * 2
    await session.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def test_a_planner_turn_whose_temporary_directory_cant_be_made_ends_in_error() -> None:
    temp_dirs = FakeTempDirs()
    message = "Couldn't make a temporary directory: disk full"
    temp_dirs.failure = TempDirFailed(message)
    session, _, launcher = session_for(roles=planning_in("none"), temp_dirs=temp_dirs)
    launcher.planner_turns = [chats]
    events = await plan(session, "Plan it.")
    assert launcher.launches == []
    assert [(e.code, e.message) for e in events if isinstance(e, ErrorOccurred)] == [
        (AGENT_FAILED, message)
    ]
    assert sole(events, TurnFinished).reason == "error"
    await plan(session, "Again.")  # The next turn makes one.
    assert cwds(launcher) == ["/tmp/orbit-1"]
    await session.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def test_a_fresh_conversation_after_a_failed_resume_keeps_the_planners_directory() -> None:
    temp_dirs = FakeTempDirs()
    chat, launcher = chat_in_none(temp_dirs, resume_lost, chats)
    chat.restore("saved-1")
    assert await chat.answer("Carry on.", Settings()) == "completed"
    assert cwds(launcher) == ["/tmp/orbit-1"] * 2
    assert (temp_dirs.made, temp_dirs.removed) == (["/tmp/orbit-1"], [])
    await chat.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def test_closing_the_planner_removes_its_directory_even_if_its_exit_cant_be_told() -> None:
    temp_dirs = FakeTempDirs()

    async def publish(event: Event) -> None:
        if isinstance(event, AgentExited):
            raise RuntimeError("The session's events are closed.")

    chat, _ = chat_in_none(temp_dirs, chats, publish=publish)
    await chat.answer("Plan it.", Settings())
    with pytest.raises(RuntimeError, match="events are closed"):
        await chat.close()
    assert temp_dirs.removed == ["/tmp/orbit-1"]


async def reviewed(
    task: TaskSpec, *scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any
) -> list[str]:
    """Where a reviewing session's agents, playing ``scripts`` or, as reviewers, ``reviewers``,
    ran ``task``, which the user added, with ``options`` for :func:`session_for`."""
    session, _, launcher = session_for(*scripts, reviewers=reviewers, review=True, **options)
    await send(session, AddTask(task=task))
    await events_until(session, idle("review"))
    await session.close()
    return [launch.cwd for launch in launcher.launches]


async def test_a_review_the_user_queues_runs_in_a_fresh_snapshot_of_the_base() -> None:
    tree = FakeTree()
    task = TaskSpec(prompt="Review it.", role=REVIEWER)
    assert await reviewed(task, reviewers=(claims_done,), tree=tree) == ["/work/review-base"]
    assert (tree.snapshots, tree.dropped) == (["base"], ["/work/review-base"])


async def test_a_role_working_in_none_in_the_review_lane_runs_in_a_temporary_directory() -> None:
    temp_dirs = FakeTempDirs()
    roles = sketcher("none", "review")
    cwds = await reviewed(sketch(), claims_done, tree=FakeTree(), roles=roles, temp_dirs=temp_dirs)
    assert cwds == ["/tmp/orbit-1"]
    assert temp_dirs.removed == ["/tmp/orbit-1"]
