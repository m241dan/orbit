"""A task naming a requirements record whose number another record took by the time it runs:
orbit moves the record to the next free number in it and in the tasks still to come."""

from collections.abc import Collection
from typing import Any

from orbit.protocol import AddTask, Event, Notice, RunTasks, TaskSpec, TurnEndReason
from tests.agent_fakes import (
    Call,
    FakeTree,
    board,
    gives_up,
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
    agent = Briefs()
    session, _, _ = session_for(agent, gives_up, file_names=files, **options)
    for task in tasks:
        await send(session, AddTask(task=task))
    await send(session, RunTasks())
    return agent, await until_idle(session)


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
