"""The levels of tool access and effort of tasks and conversations, held to those their role's
runtime accepts: refused when added, edited or set if it doesn't accept them."""

import pytest

from orbit.agent import WorkspaceHost
from orbit.protocol import (
    CODER,
    INVALID,
    AddTask,
    Command,
    CommandAccepted,
    CommandRefused,
    EditTask,
    Event,
    Settings,
    TasksChanged,
    TaskSpec,
    UpdateSettings,
)
from tests.agent_fakes import ACCEPTED, LIMITED, events_until, put_on_board, refused, send
from tests.agent_fakes import session_for as fake_session

NO_EFFORT = (
    "Role coder's runtime claude accepts no effort setting, so it can't take effort \"high\"."
)
"""Why a coder task asking for high effort is refused on a runtime taking none."""

NOT_FULL = (
    "Role coder's runtime claude accepts only tool_access \"restricted\", so it can't take"
    ' tool_access "full".'
)
"""Why a coder task asking for full tool access is refused on a runtime taking only restricted."""

BEYOND = [
    pytest.param(Settings(effort="high"), NO_EFFORT, id="effort"),
    pytest.param(Settings(tool_access="full"), NOT_FULL, id="tool_access"),
]


def session_for() -> WorkspaceHost:
    """A session whose runtime can do only what :data:`LIMITED` says."""
    session, _, launcher = fake_session()
    launcher.capabilities = LIMITED
    return session


async def answer_to(session: WorkspaceHost, command: Command) -> list[Event]:
    """The events until ``command``, sent to ``session``, is accepted or refused."""
    await send(session, command)
    return await events_until(session, lambda e: isinstance(e, CommandAccepted | CommandRefused))


@pytest.mark.parametrize(("settings", "message"), BEYOND)
async def test_a_task_added_beyond_its_runtime_is_refused(settings: Settings, message: str) -> None:
    session = session_for()
    events = await answer_to(session, AddTask(task=TaskSpec(prompt="a", settings=settings)))
    await session.close()
    assert events[-1] == refused(INVALID, message)
    assert not any(isinstance(event, TasksChanged) for event in events)


@pytest.mark.parametrize(("settings", "message"), BEYOND)
async def test_a_task_edited_beyond_its_runtime_is_refused(
    settings: Settings, message: str
) -> None:
    session = session_for()
    put_on_board(session, TaskSpec(prompt="a"))
    [state] = session._board.tasks
    edited = TaskSpec(prompt="a", settings=settings)
    events = await answer_to(session, EditTask(task_id=state.id, task=edited))
    await session.close()
    assert events[-1] == refused(INVALID, message)
    assert session._board.get(state.id).task.settings == Settings()


async def test_settings_beyond_the_conversations_runtime_are_refused() -> None:
    session = session_for()
    events = await answer_to(session, UpdateSettings(settings=Settings(effort="low")))
    await session.close()
    message = (
        "Role planner's runtime claude accepts no effort setting, so it can't take effort \"low\"."
    )
    assert events[-1] == refused(INVALID, message)
    assert session.settings == Settings()


@pytest.mark.parametrize("role", [CODER, "nobody"])
async def test_a_task_with_levels_its_runtime_accepts_is_added(role: str) -> None:
    session = session_for()
    events = await answer_to(session, AddTask(task=TaskSpec(prompt="a", role=role)))
    await session.close()
    assert events[-1] == ACCEPTED


async def test_a_task_edited_to_levels_its_runtime_accepts_is_edited() -> None:
    session = session_for()
    put_on_board(session, TaskSpec(prompt="a"))
    [state] = session._board.tasks
    events = await answer_to(session, EditTask(task_id=state.id, task=TaskSpec(prompt="b")))
    await session.close()
    assert events[-1] == ACCEPTED


async def test_settings_its_runtime_accepts_are_set() -> None:
    session = session_for()
    settings = Settings(model="opus")
    events = await answer_to(session, UpdateSettings(settings=settings))
    await session.close()
    assert events[-1] == ACCEPTED
    assert session.settings == settings
