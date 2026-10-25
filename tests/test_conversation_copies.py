"""A conversational role working in a copy: its copy is made at its conversation's first turn
and reused by the turns after it, across a switch of session and a restart; a reset removes it,
and the startup check removes the copies past their role's TTL and those left over."""

from datetime import timedelta

from orbit.agent import WorkspaceHost
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SavedCopy
from orbit.protocol import (
    PLANNER,
    ActivateSession,
    ConversationReset,
    NewSession,
    ResetConversation,
    SendMessage,
)
from tests.agent_fakes import (
    ROLES,
    START,
    FakeCopies,
    FakeStore,
    ScriptedLauncher,
    Ticks,
    agents_for,
    chats,
    events_until,
    greet,
    idle,
    send,
)
from tests.session_builders import opened, saved, store_with
from tests.waiting import eventually

EXPERIMENTER = "experimenter"
LAB = "lab"
"""A conversational role working in a copy, and the lane it answers in."""

EXPERIMENTER_ROLE = ROLES[PLANNER].model_copy(
    update={"instructions": "You experiment.", "lane": LAB, "workspace": "copy"}
)

EXPERIMENTING: dict[str, Role] = {**ROLES, EXPERIMENTER: EXPERIMENTER_ROLE}


def experimenting(store: FakeStore, copies: FakeCopies) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose experimenter works in ``copies``, saved in ``store`` as ``session-1``,
    then ``session-2`` and so on."""
    agents, launcher = agents_for(roles=EXPERIMENTING, copies=copies)
    launcher.planner_turns = [chats] * 6
    ids = iter(f"session-{n}" for n in range(1, 100))
    host = WorkspaceHost(agents=agents, store=store, clock=Ticks(), new_id=lambda: next(ids))
    return host, launcher


async def experiment(session: WorkspaceHost) -> None:
    """Message the experimenter and wait for its turn to end."""
    await send(session, SendMessage(agent=EXPERIMENTER, text="Run it."))
    await events_until(session, idle(LAB))


def directories(launcher: ScriptedLauncher) -> list[str]:
    """The directory each agent launched in, in turn."""
    return [launch.cwd for launch in launcher.launches]


async def test_a_roles_turns_run_in_the_copy_its_first_turn_made_recorded_in_the_session() -> None:
    store, copies = FakeStore(), FakeCopies()
    session, launcher = experimenting(store, copies)
    await experiment(session)
    await experiment(session)
    assert directories(launcher) == ["/copies/copy-1", "/copies/copy-1"]
    assert copies.made == ["/copies/copy-1"]
    assert store.last.copies[EXPERIMENTER].path == "/copies/copy-1"
    await session.close()
    assert copies.removed == []  # Kept for a restart.


async def test_a_reset_removes_the_copy_and_the_next_turn_makes_another() -> None:
    store, copies = FakeStore(), FakeCopies()
    session, launcher = experimenting(store, copies)
    await experiment(session)
    await send(session, ResetConversation(agent=EXPERIMENTER))
    await events_until(session, lambda e: e == ConversationReset(agent=EXPERIMENTER))
    assert (copies.removed, store.last.copies) == (["/copies/copy-1"], {})
    await experiment(session)
    assert directories(launcher)[-1] == "/copies/copy-2"
    await session.close()


async def test_a_switch_away_and_back_keeps_the_copy() -> None:
    store, copies = FakeStore(), FakeCopies()
    session, launcher = experimenting(store, copies)
    await experiment(session)
    await opened(session, NewSession())
    await opened(session, ActivateSession(session="session-1"))
    await experiment(session)
    assert directories(launcher) == ["/copies/copy-1", "/copies/copy-1"]
    assert copies.removed == []
    await session.close()


async def test_after_a_restart_the_recorded_copy_is_reused() -> None:
    copy = SavedCopy(path="/copies/earlier", last_turn=START)
    store = store_with(saved("talks", copies={EXPERIMENTER: copy}))
    copies = FakeCopies("/copies/earlier")
    session, launcher = experimenting(store, copies)
    await opened(session, ActivateSession(session="talks"))
    await experiment(session)
    assert directories(launcher) == ["/copies/earlier"]
    assert copies.made == []
    await session.close()


async def test_startup_removes_expired_and_unrecorded_copies_keeping_the_others() -> None:
    old = SavedCopy(path="/copies/old", last_turn=START - timedelta(hours=25))
    young = SavedCopy(path="/copies/young", last_turn=START - timedelta(hours=1))
    store = store_with(
        saved("a", copies={EXPERIMENTER: old}), saved("b", copies={EXPERIMENTER: young})
    )
    copies = FakeCopies("/copies/old", "/copies/young", "/copies/stray")
    session, _ = experimenting(store, copies)
    await greet(session)
    await eventually(lambda: copies.removed == ["/copies/old", "/copies/stray"])
    await session.close()
    assert copies.left == ["/copies/young"]
