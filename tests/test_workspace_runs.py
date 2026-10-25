"""Each task runs in its role's workspace: a role working in none in an empty temporary directory
gone once its task ends, however it does; one in the checkout in the workspace's root; one on a
snapshot in a fresh snapshot of the base, dropped after; the built-in roles where they always
have."""

import asyncio
from typing import Any

from orbit.agent.application.ports import TempDirFailed
from orbit.agent.domain.roles import Role, Workspace
from orbit.protocol import (
    AGENT_FAILED,
    PLANNER,
    REVIEWER,
    AddTask,
    CancelTurn,
    ErrorOccurred,
    RunTasks,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeTempDirs,
    FakeTree,
    Script,
    board,
    board_of,
    chats,
    claims_done,
    events_until,
    idle,
    plan,
    send,
    session_for,
)


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


async def test_a_planner_working_in_none_takes_each_turn_in_a_temporary_directory() -> None:
    temp_dirs = FakeTempDirs()
    planner = ROLES[PLANNER].model_copy(update={"workspace": "none"})
    session, _, launcher = session_for(roles={**ROLES, PLANNER: planner}, temp_dirs=temp_dirs)
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan it.")
    assert temp_dirs.removed == ["/tmp/orbit-1"]
    await plan(session, "And then?")
    assert [launch.cwd for launch in launcher.launches] == ["/tmp/orbit-1", "/tmp/orbit-2"]
    assert temp_dirs.removed == ["/tmp/orbit-1", "/tmp/orbit-2"]
    await session.close()


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
