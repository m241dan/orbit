"""Resetting the planner: its conversation ends, so the next prompt starts a fresh one, and its
role is read again from the project's config."""

import asyncio

from orbit.agent import AgentSession, ConfigError, Role
from orbit.agent.application.session import (
    NO_PLANNER_MESSAGE,
    NOTHING_TO_RESET,
    PLANNER_BUSY_RESET,
    ROLE_KEPT,
)
from orbit.agent.domain.verification import steps_note
from orbit.protocol import (
    AgentExited,
    CancelTurn,
    ErrorOccurred,
    Event,
    Notice,
    OpenSession,
    PlannerReset,
    ResetPlanner,
    SubmitPrompt,
)
from tests.agent_fakes import (
    ROLES,
    FakeStore,
    ScriptedLauncher,
    Stop,
    board,
    events_until,
    idle,
    of_type,
    session_for,
)
from tests.test_planner import chats, hangs, plan, proposes_two, steps_chat
from tests.test_session_switch import opened, saved, store_with, switching

NEW_ROLE = ROLES["planner"].model_copy(
    update={"instructions": "Plan small.", "tools": ("propose_task",)}
)


async def reset(session: AgentSession) -> list[Event]:
    """The events from asking to reset the planner until it's reset."""
    await session.send(ResetPlanner())
    return await events_until(session, of_type(PlannerReset))


async def refusal(session: AgentSession) -> Event:
    """What asking to reset the planner is answered with, when it's refused."""
    await session.send(ResetPlanner())
    answered = Stop("refused", lambda e: isinstance(e, ErrorOccurred | Notice))
    return (await events_until(session, answered))[-1]


async def planned_twice(launcher: ScriptedLauncher, session: AgentSession) -> None:
    """Prompt the planner, reset it, and prompt it again."""
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan.")
    await reset(session)
    await plan(session, "Plan again.")


async def test_a_reset_ends_the_planners_agent_and_its_access() -> None:
    session, host, launcher = session_for()
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    events = await reset(session)
    assert AgentExited(agent_id="agent-1", reason="finished") in events
    assert (await host.call("token-1", "propose_task", title="x", prompt="y")).is_error
    await session.close()


async def test_the_next_prompt_starts_a_fresh_conversation_told_the_steps_again() -> None:
    session, _, launcher = session_for()
    await planned_twice(launcher, session)
    fresh = launcher.launches[-1]
    assert fresh.agent.id == "agent-2"
    assert launcher.resumed == [None, None]
    assert fresh.prompt == f"{steps_note(())}\n\nPlan again."
    await session.close()


async def test_a_reset_saves_the_cleared_conversation() -> None:
    store = FakeStore()
    session, _, launcher = session_for(store=store)
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    assert store.last.planner_conversation == "conversation-agent-1"
    await reset(session)
    await asyncio.sleep(0.01)  # Saving takes a while.
    assert store.last.planner_conversation is None
    await session.close()


async def test_draft_tasks_stay_after_a_reset() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [proposes_two]
    drafts = board(await plan(session, "Plan."))
    await reset(session)
    assert session._board.tasks == drafts
    await session.close()


async def test_a_reset_rereads_the_planners_role() -> None:
    session, _, launcher = session_for(planner_role=lambda: NEW_ROLE)
    await planned_twice(launcher, session)
    fresh = launcher.launches[-1]
    assert (fresh.instructions, fresh.tools) == ("Plan small.", ("propose_task",))
    agents = session.agents
    assert agents is not None and agents.roles["coder"] == ROLES["coder"]
    await session.close()


async def test_an_invalid_config_keeps_the_planners_role_but_still_resets() -> None:
    def broken() -> Role | ConfigError:
        return ConfigError(".orbit/config.toml: roles.planner.tools: unknown tools ['fly']")

    session, _, launcher = session_for(planner_role=broken)
    launcher.planner_turns = [chats, chats]
    await plan(session, "Plan.")
    events = await reset(session)
    message = f"{ROLE_KEPT}: .orbit/config.toml: roles.planner.tools: unknown tools ['fly']"
    assert ErrorOccurred(message=message) in events
    await plan(session, "Plan again.")
    fresh = launcher.launches[-1]
    assert (fresh.agent.id, fresh.tools) == ("agent-2", ROLES["planner"].tools)
    await session.close()


async def test_a_reset_is_refused_while_the_planner_answers() -> None:
    session, _, launcher = session_for()
    launcher.planner_turns = [hangs]
    await session.send(SubmitPrompt(text="Plan."))
    while not launcher.launches:
        await asyncio.sleep(0)
    assert await refusal(session) == ErrorOccurred(message=PLANNER_BUSY_RESET)
    await session.send(CancelTurn(lane="planner"))
    events = await events_until(session, idle("planner"))
    assert not any(isinstance(e, PlannerReset | AgentExited) for e in events)
    await session.close()


async def test_before_the_first_prompt_there_is_nothing_to_reset() -> None:
    session, _, _ = session_for()
    assert await refusal(session) == Notice(message=NOTHING_TO_RESET)
    await session.close()


async def test_resetting_needs_a_planner() -> None:
    session = AgentSession()
    assert await refusal(session) == ErrorOccurred(message=NO_PLANNER_MESSAGE)
    await session.close()


async def test_a_restored_conversation_can_be_reset_before_its_first_prompt() -> None:
    chat, _, _ = steps_chat()
    assert not chat.started
    chat.restore("saved-1")
    assert chat.started


async def test_resetting_a_restored_conversation_starts_the_next_prompt_afresh() -> None:
    store = store_with(saved("auth", planner_conversation="conversation-x"))
    session, launcher = switching(store)
    await opened(session, OpenSession(session_id="auth"))
    events = await reset(session)
    assert not any(isinstance(e, AgentExited | Notice) for e in events)
    await asyncio.sleep(0.01)  # Saving takes a while.
    assert store.last.planner_conversation is None
    launcher.planner_turns = [chats]
    await plan(session, "Plan.")
    assert launcher.resumed == [None]
    assert launcher.launches[-1].prompt == f"{steps_note(())}\n\nPlan."
    await session.close()
