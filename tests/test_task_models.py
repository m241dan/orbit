"""The models of tasks and conversations, held to those their role's runtime takes: refused when
added, edited or set if their runtime doesn't take them, and resolved to the id the agent is
launched with."""

import asyncio
from dataclasses import replace

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.domain.runtimes import CLAUDE, UnknownModel
from orbit.protocol import (
    AGENT_FAILED,
    CODER,
    INVALID,
    PLANNER,
    AddTask,
    Command,
    CommandAccepted,
    CommandRefused,
    EditTask,
    ErrorOccurred,
    Event,
    RunTasks,
    SendMessage,
    Settings,
    SettingsChanged,
    TasksChanged,
    TaskSpec,
    UpdateSettings,
)
from tests.agent_fakes import (
    ACCEPTED,
    CLAUDE_MODELS,
    ROLES,
    agents_for,
    chats,
    claims_done,
    events_until,
    idle,
    put_on_board,
    refused,
    run,
    send,
    session_for,
)

FAST = replace(CLAUDE_MODELS, aliases={**CLAUDE_MODELS.aliases, "fast": "claude-haiku-5-5"})
"""Claude Code's models, with the alias ``fast`` a user config adds."""

ALIASES = (
    "the runtime claude takes the aliases fable, opus, sonnet, haiku, fast or a full id starting"
    ' with "claude-"'
)
"""What says which models :data:`FAST` takes."""

BOGUS = Settings(model="bogus")
"""Settings with a model no runtime takes."""


def bogus(role: str) -> str:
    """Why the model ``bogus`` is refused in ``role``."""
    return f"{role}'s model \"bogus\" doesn't resolve: {ALIASES}."


async def answer_to(session: WorkspaceHost, command: Command) -> list[Event]:
    """The events until ``command``, sent to ``session``, is accepted or refused."""
    await send(session, command)
    return await events_until(session, answered)


def answered(event: Event) -> bool:
    """Whether ``event`` says a command was accepted or refused."""
    return isinstance(event, CommandAccepted | CommandRefused)


async def test_a_task_added_with_a_model_its_runtime_doesnt_take_is_refused_as_invalid() -> None:
    session, _, _ = session_for(models={CLAUDE: FAST})
    events = await answer_to(session, AddTask(task=TaskSpec(prompt="a", settings=BOGUS)))
    await session.close()
    assert events[-1] == refused(INVALID, bogus(CODER))
    assert not any(isinstance(event, TasksChanged) for event in events)


@pytest.mark.parametrize("model", ["fast", "claude-sonnet-5-5", None])
async def test_a_task_added_with_an_alias_a_full_id_or_no_model_is_accepted(
    model: str | None,
) -> None:
    session, _, _ = session_for(models={CLAUDE: FAST})
    task = TaskSpec(prompt="a", settings=Settings(model=model))
    events = await answer_to(session, AddTask(task=task))
    await session.close()
    assert events[-1] == ACCEPTED


async def test_a_task_edited_to_a_model_its_runtime_doesnt_take_is_refused_as_invalid() -> None:
    session, _, _ = session_for(models={CLAUDE: FAST})
    put_on_board(session, TaskSpec(prompt="a"))
    [state] = session._board.tasks
    edited = TaskSpec(prompt="a", settings=BOGUS)
    events = await answer_to(session, EditTask(task_id=state.id, task=edited))
    await session.close()
    assert events[-1] == refused(INVALID, bogus(CODER))
    assert session._board.get(state.id).task.settings.model is None


async def test_settings_with_a_model_the_roles_runtime_doesnt_take_are_refused() -> None:
    session, _, _ = session_for(models={CLAUDE: FAST})
    events = await answer_to(session, UpdateSettings(settings=BOGUS))
    await session.close()
    assert events[-1] == refused(INVALID, bogus(PLANNER))
    assert session.settings == Settings()


async def test_settings_changed_carry_the_roles_runtimes_aliases_with_their_ids() -> None:
    session, _, _ = session_for(models={CLAUDE: FAST})
    events = await answer_to(session, UpdateSettings(settings=Settings(model="fast")))
    await session.close()
    [changed] = [event for event in events if isinstance(event, SettingsChanged)]
    assert changed.model_choices == ("fable", "opus", "sonnet", "haiku", "fast")
    assert changed.model_ids == dict(FAST.aliases)


async def test_an_agent_is_launched_with_the_id_its_tasks_alias_resolves_to() -> None:
    session, _, launcher = session_for(claims_done, models={CLAUDE: FAST})
    task = TaskSpec(prompt="a", settings=Settings(model="fast"))
    await asyncio.wait_for(run(session, task), 5)
    await session.close()
    [launch] = launcher.launches
    assert launch.settings.model == "claude-haiku-5-5"


async def test_the_planner_is_launched_with_the_id_its_alias_resolves_to() -> None:
    session, _, launcher = session_for(models={CLAUDE: FAST})
    launcher.planner_turns = [chats]
    await send(session, UpdateSettings(settings=Settings(model="fast")))
    await send(session, SendMessage(agent=PLANNER, text="hello"))
    await events_until(session, idle(PLANNER))
    await session.close()
    [launch] = launcher.launches
    assert launch.settings.model == "claude-haiku-5-5"


async def test_an_agent_whose_saved_model_no_longer_resolves_fails_saying_why() -> None:
    session, _, launcher = session_for(claims_done, models={CLAUDE: FAST})
    put_on_board(session, TaskSpec(prompt="a", settings=Settings(model="gone")))
    await send(session, RunTasks())
    events = await events_until(session, idle())
    await session.close()
    message = f'The model "gone" doesn\'t resolve: {ALIASES}.'
    assert ErrorOccurred(agent_id="agent-1", code=AGENT_FAILED, message=message) in events
    assert launcher.launches == []


async def test_a_planner_given_a_model_that_doesnt_resolve_fails_its_turn_saying_why() -> None:
    agents, launcher = agents_for(models={CLAUDE: FAST})
    session = WorkspaceHost(Settings(model="gone"), agents=agents)
    launcher.planner_turns = [chats]
    await send(session, SendMessage(agent=PLANNER, text="hello"))
    events = await events_until(session, idle(PLANNER))
    await session.close()
    message = f'The model "gone" doesn\'t resolve: {ALIASES}.'
    assert [e.message for e in events if isinstance(e, ErrorOccurred)] == [message]
    assert launcher.launches == []


def test_a_model_is_given_as_it_is_without_its_runtimes_models() -> None:
    agents, _ = agents_for()
    assert agents.launch_model(CODER, "anything") == "anything"
    assert agents.model_aliases(CODER) is None


def test_a_role_there_is_none_of_has_no_models_and_its_model_is_given_as_it_is() -> None:
    agents, _ = agents_for(models={CLAUDE: FAST})
    assert agents.model_aliases("nobody") is None
    assert agents.launch_model("nobody", "anything") == "anything"


def test_no_model_is_launched_as_none() -> None:
    agents, _ = agents_for(models={CLAUDE: FAST})
    assert agents.launch_model(CODER, None) is None


def test_a_model_its_runtime_doesnt_take_isnt_launched() -> None:
    agents, _ = agents_for(models={CLAUDE: FAST}, roles=ROLES)
    assert agents.model_aliases(CODER) == FAST
    with pytest.raises(UnknownModel):
        agents.launch_model(CODER, "bogus")
