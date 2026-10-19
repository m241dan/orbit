"""A read-only session: another orbit owns the repository, so the work asked for is queued."""

import asyncio

import pytest

from orbit.agent import AgentSession
from orbit.agent.domain.ownership import WorkspaceOwner, read_only_note
from orbit.protocol import (
    CONFIGURE_CHECKS,
    PLANNER,
    SUPER_REVIEW,
    AddTask,
    AssistantMessage,
    Catalog,
    Command,
    CommandAccepted,
    CommandRefused,
    FireTrigger,
    Notice,
    RunTasks,
    SendMessage,
    Snapshot,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    ACCEPTED,
    Call,
    FakeTree,
    Steps,
    events_until,
    greeted,
    idle,
    lane_of,
    of_type,
    queued,
    send,
    session_for,
    sole,
    startup_of,
    verified,
)

OWNER = WorkspaceOwner(pid=42)


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Done")
    return "completed"


async def chats(call: Call) -> TurnEndReason:
    return "completed"


async def test_a_read_only_session_says_so_in_its_catalog_and_snapshot_then_names_the_owner() -> (
    None
):
    session, _, _ = session_for(owner=OWNER)
    await greeted(session)
    events = queued(session)

    snapshot = sole(events, Snapshot)
    assert sole(events, Catalog).read_only and snapshot.read_only and session.read_only
    note = Notice(message=read_only_note(OWNER))
    assert events.index(note) > events.index(snapshot)
    await session.close()


async def test_a_session_owning_the_repository_isnt_read_only() -> None:
    session, _, _ = session_for()
    await greeted(session)
    events = queued(session)

    assert not any(e.read_only for e in events if isinstance(e, Catalog | Snapshot))
    assert not any(isinstance(e, Notice) and e.message.startswith("Read-only") for e in events)
    assert not session.read_only
    await session.close()


async def test_work_asked_of_a_read_only_session_is_accepted_and_queued_not_run() -> None:
    session, _, launcher = session_for(claims_done, owner=OWNER, tree=FakeTree(), review=True)
    await send(session, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(session, RunTasks())
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    await asyncio.sleep(0.05)

    events = queued(session)
    assert events.count(ACCEPTED) == 3
    assert session.queued_work == (RunTasks(), FireTrigger(trigger=SUPER_REVIEW))
    assert launcher.launches == []
    await session.close()


async def refusal_of(session: AgentSession, command: Command) -> CommandRefused:
    """What ``session`` answered ``command`` with, which must be a refusal."""
    await send(session, command)
    events = await events_until(session, of_type(CommandRefused))
    refused = events[-1]
    assert isinstance(refused, CommandRefused)
    assert not any(isinstance(e, CommandAccepted) for e in events)
    return refused


INVALID_WORK = [
    pytest.param(FireTrigger(trigger="bogus"), id="unknown-trigger"),
    pytest.param(FireTrigger(trigger=SUPER_REVIEW, input={"x": "1"}), id="trigger-input"),
    pytest.param(FireTrigger(trigger=SUPER_REVIEW), id="super-review-without-reviews"),
    pytest.param(FireTrigger(trigger=CONFIGURE_CHECKS), id="configure-without-verification"),
]


@pytest.mark.parametrize("command", INVALID_WORK)
async def test_a_read_only_session_refuses_invalid_work_as_an_owning_one_does(
    command: RunTasks | FireTrigger,
) -> None:
    owning, _, _ = session_for(tree=FakeTree())
    read_only, _, _ = session_for(tree=FakeTree(), owner=OWNER)

    expected = await refusal_of(owning, command)
    assert await refusal_of(read_only, command) == expected
    assert read_only.queued_work == ()
    await owning.close()
    await read_only.close()


@pytest.mark.parametrize("command", [RunTasks(), FireTrigger(trigger=CONFIGURE_CHECKS)])
async def test_a_read_only_session_without_agents_refuses_work_as_an_owning_one_does(
    command: RunTasks | FireTrigger,
) -> None:
    owning, read_only = AgentSession(), AgentSession(owner=OWNER)

    expected = await refusal_of(owning, command)
    assert await refusal_of(read_only, command) == expected
    assert read_only.queued_work == ()
    await owning.close()
    await read_only.close()


async def test_the_planner_still_answers_a_read_only_session() -> None:
    session, _, launcher = session_for(owner=OWNER)
    launcher.planner_turns = [chats]
    launcher.planner_replies = ["Hello."]
    await send(session, SendMessage(agent=PLANNER, text="Hi."))
    events = await events_until(session, idle(lane=None))

    assert any(isinstance(e, AssistantMessage) and e.text == "Hello." for e in events)
    assert any(isinstance(e, CommandAccepted) for e in events)
    await session.close()


async def test_a_read_only_session_runs_no_startup_check() -> None:
    session, _, _ = session_for(
        verification=verified(Steps(True)), check_at_startup=True, owner=OWNER
    )
    await greeted(session)
    assert startup_of(session) is None
    await session.close()


async def test_a_read_only_session_runs_no_reviews() -> None:
    session, _, _ = session_for(owner=OWNER, tree=FakeTree(), review=True)
    lane = lane_of(session)
    lane.kick()
    assert not lane.running
    await session.close()


def test_a_session_given_no_owner_queues_nothing() -> None:
    assert AgentSession().queued_work == ()
