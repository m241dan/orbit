"""Skills the tasks orbit makes from others inherit: a review names the skills of the tasks it
reviews, and its reviewer gets its lenses and those; a super review names none; a reviewer's
suggestion names its review's; a fallback fix names the failing task's; and the startup check's
fix task, which comes from no task, names none."""

import asyncio
from collections.abc import Mapping

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.skills import Skill, SkillLibrary
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    REVIEWER,
    SUPER_LENS_REVIEWERS,
    AddTask,
    RunTasks,
    TaskSpec,
    TaskState,
)
from tests.agent_fakes import (
    ROLES,
    TRIGGERS,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    agent_exited,
    board,
    checked,
    claims_done,
    events_until,
    keeps_claiming,
    run,
    send,
    session_for,
    settle,
    suggests,
    super_every,
    verified,
)

LIBRARY = SkillLibrary(
    skills={
        name: Skill(name=name, description=f"{name}.", body=f"Be {name}.\n", files={})
        for name in (*ROLES[REVIEWER].skills, "python", "application", "cpp")
    }
)
"""The skills a session's agents can be given: the reviewer's lenses and three more."""

CHOSEN = TaskSpec(prompt="Add a flag.", skills=("python", "application"))
"""A coder's task naming the skills ``python`` and ``application``."""


async def reviewed(
    *reviewers: Script, triggers: Mapping[str, Trigger] = TRIGGERS
) -> tuple[list[TaskState], ScriptedLauncher]:
    """The tasks on the board once :data:`CHOSEN` has landed and been reviewed by
    ``reviewers``, with ``triggers``, and the suggestions they made have run and been reviewed in
    turn; and the launcher that played them."""
    session, _, launcher = session_for(
        claims_done,
        claims_done,
        reviewers=reviewers,
        tree=FakeTree(),
        review=True,
        triggers=triggers,
        skill_library=LIBRARY,
    )
    await send(session, AddTask(task=CHOSEN))
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    return list(board(events)), launcher


def headings(instructions: str) -> list[str]:
    """The skills ``instructions`` deliver, by the headings of their sections, in order."""
    return [line.removeprefix("### ") for line in instructions.splitlines() if line[:4] == "### "]


async def test_a_review_names_the_skills_of_the_task_it_reviews() -> None:
    tasks, _ = await reviewed(suggests())
    assert [(t.task.role, t.task.skills) for t in tasks] == [
        (CODER, ("python", "application")),
        (REVIEWER, ("python", "application")),
    ]


async def test_the_reviewer_is_launched_with_its_lenses_then_the_reviewed_tasks_skills() -> None:
    _, launcher = await reviewed(suggests())
    [launch] = [la for la in launcher.launches if la.agent.role == REVIEWER]
    assert headings(launch.instructions) == [*ROLES[REVIEWER].skills, "python", "application"]


async def test_a_suggestion_names_the_skills_of_the_review_that_made_it() -> None:
    tasks, _ = await reviewed(suggests("Add tests"), suggests())
    [suggestion] = [t for t in tasks if t.batch is not None]
    assert suggestion.task.skills == ("python", "application")


async def test_the_review_of_a_batch_names_the_skills_of_its_suggestions() -> None:
    tasks, _ = await reviewed(suggests("Add tests"), suggests())
    batch_review = tasks[-1]
    assert batch_review.task.prompt.startswith("Review the 1 commits made for review batch-1")
    assert batch_review.task.skills == ("python", "application")


async def test_a_super_review_names_no_skills_after_a_task_naming_some_lands() -> None:
    tasks, _ = await reviewed(suggests(), suggests(), triggers=super_every(1))
    lenses = [t for t in tasks if t.task.role in SUPER_LENS_REVIEWERS]
    assert [t.task.skills for t in lenses] == [()] * len(SUPER_LENS_REVIEWERS)


async def test_a_fallback_fix_names_the_skills_of_the_task_it_fixes() -> None:
    session, _, _ = session_for(
        keeps_claiming,
        claims_done,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
        skill_library=LIBRARY,
    )
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="one", skills=("cpp",))), 5)
    await session.close()
    one, fix = board(events)
    assert (fix.fixes, fix.task.skills) == (one.id, ("cpp",))


async def test_the_startup_checks_fix_task_names_no_skills() -> None:
    session, _, _ = session_for(
        verification=checked(Steps(False)),
        tree=FakeTree(),
        check_at_startup=True,
        skill_library=LIBRARY,
    )
    events = await events_until(session, agent_exited("startup-check"))
    await session.close()
    [draft] = board(events)
    assert (draft.status, draft.task.skills) == ("draft", ())
