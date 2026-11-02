"""The super review fired from its trigger in a session: due every so many landings of the
user's and the planner's tasks, as the trigger's ``every`` says (or ``super_review_every``, its
alias), not counting review-suggested work, fired by hand with ctrl+g, its count starting again
either way, and queued from the trigger's template with the trigger's name as its origin, one
task for each of the super review's lenses."""

import asyncio
from collections.abc import Mapping
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.domain.config import SUPER_REVIEW_EVERY_MOVING, Package, load_layered
from orbit.agent.domain.reviews import ReviewJob, SuperReviewCount, review_prompt
from orbit.agent.domain.roles import Role
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    DRIFT_REVIEWER,
    REVIEWER,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    UNKNOWN,
    CommandAccepted,
    CommandRefused,
    FireTrigger,
    TaskSpec,
    TaskState,
)
from tests.agent_fakes import (
    ROLES,
    TRIGGERS,
    FakeTree,
    board,
    claims_done,
    events_until,
    lane_of,
    refused,
    send,
    session_for,
    settle,
    settled,
    start,
    suggests,
    super_every,
)


def unreviewed(triggers: Mapping[str, Trigger]) -> dict[str, Trigger]:
    """``triggers`` with the review trigger disabled, so only super reviews are queued."""
    off = triggers["review"].model_copy(update={"enabled": False})
    return {**triggers, "review": off}


def configured(data: dict[str, Any]) -> tuple[dict[str, Trigger], tuple[str, ...]]:
    """The triggers a project file holding ``data`` makes over the package's, and the notices
    loading it gives."""
    config = load_layered(data, Package(ROLES, TRIGGERS), path="/repo/.orbit/config.toml").config
    return dict(config.triggers), config.notices


ARCH = "arch-reviewer"
"""A project's own reviewer, which a project may give the super review instead of its lenses."""

ARCH_SUPER = {**TRIGGERS, SUPER_REVIEW: TRIGGERS[SUPER_REVIEW].model_copy(update={"role": ARCH})}
""":data:`TRIGGERS` with the super review overridden to queue a task for :data:`ARCH` alone."""


def arch_roles(*disabled: str) -> dict[str, Role]:
    """The built-in roles and :data:`ARCH`, with those ``disabled``."""
    roles = {**ROLES, ARCH: ROLES[DRIFT_REVIEWER]}
    return {
        name: role.model_copy(update={"enabled": name not in disabled})
        for name, role in roles.items()
    }


LENSES = (claims_done,) * len(SUPER_LENS_REVIEWERS)
"""The scripts of one super review's lenses, each finding nothing."""


def reviewing(*scripts: Any, **options: Any) -> WorkspaceHost:
    """A session landing work in a tree and reviewing it, its tasks played by ``scripts``, then
    the lenses of a super review by :data:`LENSES`, and its reviews by reviewers claiming done;
    ``options`` go to :func:`session_for`."""
    reviewers = options.pop("reviewers", (suggests(),) * 3)
    session, _, _ = session_for(
        *scripts, *LENSES, reviewers=reviewers, tree=FakeTree(), review=True, **options
    )
    return session


async def landed(count: int, triggers: Mapping[str, Trigger]) -> list[TaskState]:
    """The tasks a session with ``triggers`` ends with once ``count`` of the user's tasks have
    landed, and what they triggered has run."""
    session = reviewing(*[claims_done] * count, triggers=triggers)
    await start(session, *(f"Task {n}" for n in range(1, count + 1)))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    return list(board(events))


def supers(tasks: list[TaskState]) -> list[TaskState]:
    """The super review's lens tasks among ``tasks``."""
    return [state for state in tasks if state.task.role in SUPER_LENS_REVIEWERS]


def whole(commit: str) -> ReviewJob:
    """The super review of the codebase at ``commit``."""
    return ReviewJob(commits=(commit,), round=0, batch=None, whole=True)


def lens_prompt(commit: str) -> str:
    """Today's super review prompt of the codebase at ``commit`` without its cap on
    suggestions, as a lens files none."""
    prompt = review_prompt(whole(commit), "", 10, ())
    return prompt.replace("\n\nYou may make at most 10 suggestions.", "", 1)


def described(tasks: list[TaskState]) -> list[tuple[str, str, str, str | None, str]]:
    """Each of ``tasks``' role, origin, what it was queued as, model and prompt."""
    return [
        (t.task.role, t.origin, t.queued_as, t.task.settings.model, t.task.prompt) for t in tasks
    ]


async def test_ten_landings_of_the_users_and_the_planners_tasks_queue_todays_super_review() -> None:
    session = reviewing(*[claims_done] * 10, triggers=unreviewed(TRIGGERS))
    for n in range(1, 6):
        planned = session._board.add_draft(TaskSpec(prompt=f"Planned {n}"), "planner")
        session._board.accept(planned)
    await start(session, *(f"Task {n}" for n in range(6, 11)))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    tasks = list(board(events))
    assert [state.origin for state in tasks[:10]] == ["planner"] * 5 + ["manual"] * 5
    lenses = supers(tasks)
    assert tasks[10:] == lenses
    prompt = lens_prompt("c10")
    assert described(lenses) == [
        (role, SUPER_REVIEW, "review", "opus", prompt) for role in SUPER_LENS_REVIEWERS
    ]
    assert lane_of(session).planner.super_count == SuperReviewCount(0, "c10", 1)


async def test_review_suggested_work_landing_does_not_count_toward_the_super_review() -> None:
    session = reviewing(
        claims_done,
        claims_done,
        reviewers=(suggests("Tidy the parser"), suggests()),
        triggers=super_every(2),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    tasks = board(events)
    assert [(state.task.role, state.origin) for state in tasks] == [
        (CODER, "manual"),
        (REVIEWER, "review"),
        (CODER, "review"),
        (REVIEWER, "review"),
    ]
    assert lane_of(session).planner.super_count.since_super == 1


async def test_the_triggers_every_decides_how_many_landings_a_super_review_waits_for() -> None:
    triggers, notices = configured({"triggers": {SUPER_REVIEW: {"every": 3}}})
    assert notices == ()
    lenses = supers(await landed(3, unreviewed(triggers)))
    assert [state.task.prompt for state in lenses] == [lens_prompt("c3")] * 3


async def test_super_review_every_alone_decides_it_through_the_trigger_with_its_notice() -> None:
    triggers, notices = configured({"super_review_every": 4})
    assert notices == (SUPER_REVIEW_EVERY_MOVING,)
    assert supers(await landed(3, unreviewed(triggers))) == []
    lenses = supers(await landed(4, unreviewed(triggers)))
    assert len(lenses) == 3
    assert lenses[0].task.prompt.startswith("Super review of the codebase at c4")


async def test_ctrl_g_queues_a_super_review_at_once_and_starts_the_count_again() -> None:
    session = reviewing(claims_done, claims_done, triggers=unreviewed(super_every(2)))
    await start(session, "One")
    events = await asyncio.wait_for(settle(session), 10)
    assert lane_of(session).planner.super_count.since_super == 1

    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session, events), 10)
    lenses = supers(list(board(events)))
    assert [(t.task.role, t.origin, t.status) for t in lenses] == [
        (role, SUPER_REVIEW, "completed") for role in SUPER_LENS_REVIEWERS
    ]
    assert lane_of(session).planner.super_count == SuperReviewCount(0, "base", 1)

    await start(session, "Two")  # One landing since: not yet due again.
    events = await asyncio.wait_for(settle(session, events), 10)
    await session.close()
    assert len(supers(list(board(events)))) == 3
    assert lane_of(session).planner.super_count.since_super == 1


async def test_ctrl_g_with_the_super_review_trigger_disabled_is_refused_queuing_nothing() -> None:
    off = TRIGGERS[SUPER_REVIEW].model_copy(update={"enabled": False})
    session = reviewing(triggers={**TRIGGERS, SUPER_REVIEW: off})
    lane_of(session).planner.counts = {SUPER_REVIEW: 4}
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await events_until(session, lambda event: isinstance(event, CommandRefused))
    await settled()
    message = "No trigger 'super-review'; the triggers are configure-checks."
    assert events[-1] == refused(UNKNOWN, message)
    await session.close()
    assert session._board.tasks == ()
    assert lane_of(session).planner.super_count == SuperReviewCount(4)


async def test_a_super_review_asked_of_the_lane_with_its_trigger_disabled_queues_nothing() -> None:
    off = TRIGGERS[SUPER_REVIEW].model_copy(update={"enabled": False})
    session = reviewing(triggers={**TRIGGERS, SUPER_REVIEW: off})
    lane_of(session).planner.counts = {SUPER_REVIEW: 4}
    await lane_of(session).super_review()
    await settled()
    await session.close()
    assert session._board.tasks == ()
    assert lane_of(session).planner.super_count == SuperReviewCount(4)


async def test_ctrl_g_with_the_super_review_given_an_enabled_role_queues_that_roles_task() -> None:
    roles = arch_roles(*SUPER_LENS_REVIEWERS)
    session = reviewing(triggers=unreviewed(ARCH_SUPER), roles=roles)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await events_until(session, lambda event: isinstance(event, CommandAccepted))
    events = await asyncio.wait_for(settle(session, events), 10)
    await session.close()
    assert [(t.task.role, t.origin) for t in board(events)] == [(ARCH, SUPER_REVIEW)]


async def test_ctrl_g_with_the_super_review_given_a_disabled_role_is_refused() -> None:
    session = reviewing(triggers=unreviewed(ARCH_SUPER), roles=arch_roles(ARCH))
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await events_until(session, lambda event: isinstance(event, CommandRefused))
    await settled()
    await session.close()
    message = "No trigger 'super-review'; the triggers are configure-checks."
    assert events[-1] == refused(UNKNOWN, message)
    assert session._board.tasks == ()
