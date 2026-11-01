"""Tasks' skills checked as the user adds and edits tasks: a task naming a skill that isn't one
is refused, naming it and listing the skills, and changes nothing; one naming skills there are,
or none, is taken with them."""

from orbit.agent import WorkspaceHost
from orbit.agent.domain.skills import Skill, SkillLibrary
from orbit.protocol import (
    UNKNOWN,
    AddTask,
    Command,
    CommandAccepted,
    CommandRefused,
    EditTask,
    Event,
    TasksChanged,
    TaskSpec,
)
from tests.agent_fakes import ACCEPTED, events_until, put_on_board, refused, send, session_for

LIBRARY = SkillLibrary(
    skills={
        name: Skill(name=name, description=f"{name}.", body=f"Be {name}.\n", files={})
        for name in ("python", "application", "cpp")
    }
)
"""The skills a session's agents can be given, listed in name order."""

PYTON = "Task skills names pyton, which isn't a skill; the skills are application, cpp, python."
"""Why a task naming the skill ``pyton`` is refused."""


async def answer_to(session: WorkspaceHost, command: Command) -> list[Event]:
    """The events until ``command``, sent to ``session``, is accepted or refused."""
    await send(session, command)
    return await events_until(session, answered)


def answered(event: Event) -> bool:
    """Whether ``event`` says a command was accepted or refused."""
    return isinstance(event, CommandAccepted | CommandRefused)


async def test_a_task_added_naming_a_skill_that_isnt_one_is_refused_adding_nothing() -> None:
    session, _, _ = session_for(skill_library=LIBRARY)
    task = TaskSpec(prompt="a", skills=("python", "pyton"))
    events = await answer_to(session, AddTask(task=task))
    await session.close()
    assert events[-1] == refused(UNKNOWN, PYTON)
    assert not any(isinstance(event, TasksChanged) for event in events)
    assert session._board.tasks == ()


async def test_a_task_added_naming_skills_there_are_is_added_with_them() -> None:
    session, _, _ = session_for(skill_library=LIBRARY)
    task = TaskSpec(prompt="a", skills=("python", "application"))
    events = await answer_to(session, AddTask(task=task))
    await session.close()
    assert events[-1] == ACCEPTED
    [state] = session._board.tasks
    assert state.task.skills == ("python", "application")


async def test_a_task_edited_to_a_skill_that_isnt_one_is_refused_leaving_it_unchanged() -> None:
    session, _, _ = session_for(skill_library=LIBRARY)
    put_on_board(session, TaskSpec(prompt="a", skills=("python",)))
    [state] = session._board.tasks
    edited = TaskSpec(prompt="b", skills=("pyton",))
    events = await answer_to(session, EditTask(task_id=state.id, task=edited))
    await session.close()
    assert events[-1] == refused(UNKNOWN, PYTON)
    assert session._board.get(state.id) == state


async def test_a_task_edited_to_skills_there_are_takes_them() -> None:
    session, _, _ = session_for(skill_library=LIBRARY)
    put_on_board(session, TaskSpec(prompt="a", skills=("python", "application")))
    [state] = session._board.tasks
    edited = TaskSpec(prompt="a", skills=("python", "cpp"))
    events = await answer_to(session, EditTask(task_id=state.id, task=edited))
    await session.close()
    assert events[-1] == ACCEPTED
    assert session._board.get(state.id).task.skills == ("python", "cpp")


async def test_a_task_naming_no_skills_is_added_in_a_session_without_any() -> None:
    session, _, _ = session_for()
    events = await answer_to(session, AddTask(task=TaskSpec(prompt="a")))
    await session.close()
    assert events[-1] == ACCEPTED
