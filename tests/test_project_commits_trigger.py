"""A project's own ``commits`` trigger in a session: its task booked as a review of the whole
codebase, whose suggestions take the trigger's name as their origin, as the super review's take
``super-review``, so their landings don't count toward it; and its review, as any review orbit
queued, can't be edited."""

import asyncio
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.domain.config import Package, load_layered
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    INVALID,
    REVIEWER,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    EditTask,
    TaskSpec,
    TaskState,
)
from tests.agent_fakes import (
    ROLES,
    TRIGGERS,
    FakeTree,
    board,
    claims_done,
    lane_of,
    refused,
    send,
    session_for,
    settle,
    start,
    suggests,
)

AUDIT = "audit"


def with_audit(super_every: int | None = None, **fields: Any) -> dict[str, Trigger]:
    """The triggers a project file makes that disables the review, sets the super review's
    ``every`` to ``super_every``, if given, and adds ``audit``, a ``commits`` trigger queuing a
    reviewer's task on every landing, which files its suggestions itself, with ``fields`` over
    that."""
    audit = {"on": "commits", "role": REVIEWER, "prompt": "Audit at {commit}.", "every": 1}
    supers = {} if super_every is None else {SUPER_REVIEW: {"every": super_every}}
    project = {"review": {"enabled": False}, AUDIT: audit | fields, **supers}
    package = Package(ROLES, TRIGGERS)
    config = load_layered({"triggers": project}, package, path="/repo/.orbit/config.toml").config
    return dict(config.triggers)


def reviewing(*scripts: Any, triggers: dict[str, Trigger], reviewers: Any) -> WorkspaceHost:
    """A session landing work in a tree and reviewing it, with ``triggers``, its tasks played by
    ``scripts`` and its reviews by ``reviewers``."""
    session, _, _ = session_for(
        *scripts, reviewers=reviewers, tree=FakeTree(), review=True, triggers=triggers
    )
    return session


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str | None]]:
    """Each of ``tasks``' role, origin and batch."""
    return [(state.task.role, state.origin, state.batch) for state in tasks]


async def test_one_commits_firings_reviews_share_a_batch_a_suggestion_from_its_trigger() -> None:
    triggers = with_audit(when={"origin": ["manual", "planner"]}, super_every=1)
    lenses = (claims_done,) * len(SUPER_LENS_REVIEWERS)
    session = reviewing(
        claims_done,
        *lenses,
        claims_done,
        triggers=triggers,
        reviewers=(suggests("Split the lexer"),),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None),
        *((role, SUPER_REVIEW, None) for role in SUPER_LENS_REVIEWERS),
        (REVIEWER, AUDIT, None),
        (CODER, AUDIT, "batch-1"),
    ]


async def test_a_commits_triggers_suggestion_landing_does_not_count_toward_it() -> None:
    reviewers = (suggests("Tidy the parser"), suggests())
    session = reviewing(claims_done, claims_done, triggers=with_audit(), reviewers=reviewers)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None),
        (REVIEWER, AUDIT, None),
        (CODER, AUDIT, "batch-1"),
    ]
    assert [state.status for state in board(events)] == ["completed"] * 3
    assert lane_of(session).planner.counts[AUDIT] == 0


async def test_a_commits_triggers_review_cant_be_edited() -> None:
    session = reviewing(claims_done, triggers=with_audit(), reviewers=(suggests(),))
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    audit = TaskSpec(prompt="Audit again.", role=REVIEWER)
    await send(session, EditTask(task_id="2", task=audit))
    events = await asyncio.wait_for(settle(session, events), 10)
    await session.close()

    assert refused(INVALID, "Reviews can't be edited; remove it instead.") in events
    [_, review] = board(events)
    assert (review.origin, review.task.prompt) == (AUDIT, "Audit at c1.")
