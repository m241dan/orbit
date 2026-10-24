"""A task naming a requirements record whose number another record took by the time it runs:
orbit moves the record to the next free number in it and in the tasks still to come."""

from collections.abc import Collection
from typing import Any

from orbit.agent.domain.records import planner_note
from orbit.protocol import PLANNER, AddTask, Event, Notice, RunTasks, TaskSpec, TurnEndReason
from tests.agent_fakes import (
    ROLES,
    Call,
    FakeTree,
    ScriptedLauncher,
    board,
    chats,
    gives_up,
    plan,
    send,
    session_for,
    until_idle,
)

OLD, NEW = "docs/00024-feature-metrics.md", "docs/00025-feature-metrics.md"
WRITE = TaskSpec(prompt=f"Write the metrics record\n\nCreate `{OLD}` with exactly this text.")
LATER = TaskSpec(prompt=f"Export metrics\n\nPlan: {OLD}")
TAKEN = ("00023-bugfix-pause.md", "00024-bugfix-review-loop.md", "protocol.md")


class Files:
    """The files in each directory, as scripted, recording the directories it's asked about."""

    def __init__(self, listings: dict[str, Collection[str]]) -> None:
        self.listings = listings
        self.asked: list[str] = []

    def __call__(self, path: str) -> Collection[str]:
        self.asked.append(path)
        return self.listings.get(path, ())


class Briefs:
    """An agent that records the brief it's given and gives up."""

    def __init__(self) -> None:
        self.briefs: list[str] = []

    async def __call__(self, call: Call) -> TurnEndReason:
        self.briefs.append((await call("get_task")).text)
        return await gives_up(call)


async def run(files: Files, *tasks: TaskSpec, **options: Any) -> tuple[Briefs, list[Event]]:
    """Run ``tasks`` with ``files`` listing the directories; the first task's agent's brief
    and every event until the work lane goes idle."""
    agent, events, _ = await run_then_plan(files, *tasks, **options)
    return agent, events


async def run_then_plan(
    files: Files, *tasks: TaskSpec, **options: Any
) -> tuple[Briefs, list[Event], ScriptedLauncher]:
    """Run ``tasks`` as :func:`run` does, with the session's launcher, which plays a planner
    that chats."""
    agent = Briefs()
    session, _, launcher = session_for(agent, gives_up, file_names=files, **options)
    launcher.planner_turns = [chats, chats]
    for task in tasks:
        await send(session, AddTask(task=task))
    await send(session, RunTasks())
    events = await until_idle(session)
    if PLANNER in options.get("roles", ROLES):
        await plan(session, "Revise the plan.")
        await plan(session, "And again.")
    return agent, events, launcher


async def test_a_record_whose_number_was_taken_moves_in_its_task_and_those_to_come() -> None:
    files = Files({"/work/coder/docs": TAKEN})
    agent, events = await run(files, WRITE, LATER, tree=FakeTree())
    assert NEW in agent.briefs[0] and OLD not in agent.briefs[0]
    assert [t.task.prompt for t in board(events)] == [
        WRITE.prompt.replace(OLD, NEW),
        LATER.prompt.replace(OLD, NEW),
    ]
    assert Notice(message=f"Renamed {OLD} to {NEW}: another record took its number.") in events
    assert files.asked[0] == "/work/coder/docs"


async def test_a_record_whose_number_is_free_keeps_it() -> None:
    files = Files({"/work/coder/docs": ("00023-bugfix-pause.md",)})
    agent, events = await run(files, WRITE, tree=FakeTree())
    assert OLD in agent.briefs[0]
    assert not [e for e in events if isinstance(e, Notice)]


async def test_without_a_tree_the_workspace_itself_is_listed() -> None:
    files = Files({"/repo/docs": TAKEN})
    agent, _ = await run(files, WRITE, root="/repo")
    assert NEW in agent.briefs[0]
    assert files.asked == ["/repo/docs"]


async def test_a_task_naming_no_record_lists_nothing() -> None:
    files = Files({})
    await run(files, TaskSpec(prompt="Add a flag."), tree=FakeTree())
    assert files.asked == []


async def test_the_planner_hears_of_a_moved_record_on_its_next_turn_only() -> None:
    files = Files({"/work/coder/docs": TAKEN})
    _, _, launcher = await run_then_plan(files, WRITE, tree=FakeTree())
    first, second = (launch.prompt for launch in launcher.launches[-2:])
    assert first.endswith(f"{planner_note(OLD, NEW)}\n\nRevise the plan.")
    assert second == "And again."


async def test_a_record_moves_in_a_session_without_a_planner() -> None:
    roles = {name: role for name, role in ROLES.items() if name != PLANNER}
    agent, _ = await run(Files({"/work/coder/docs": TAKEN}), WRITE, tree=FakeTree(), roles=roles)
    assert NEW in agent.briefs[0]
