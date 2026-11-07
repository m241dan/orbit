"""The super review's lens reviewers orbit ships: one firing of ``super-review`` queues the
Style, Drift and Scaling lens tasks, whose findings one merge pass files under the super review's
cap; a disabled lens is left out; a batch filed from a lens's finding is reviewed by that lens,
through ``super-fix-review``, with Bugs and Tests; and a task a session saved waiting for the
``super-reviewer`` is cancelled as it opens."""

import asyncio
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.domain.endings import QUEUED_FOR_RETIRED_ROLE
from orbit.agent.domain.reviews import (
    BatchSnapshot,
    LaneSnapshot,
    PlannerSnapshot,
    ReviewJob,
    ReviewLimits,
)
from orbit.agent.domain.sessions import SessionRecord, parse_record, record_json
from orbit.protocol import (
    BUGS_REVIEWER,
    CODER,
    DRIFT_REVIEWER,
    REVIEWER,
    SCALING_REVIEWER,
    STYLE_REVIEWER,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    TESTS_REVIEWER,
    ActivateSession,
    AgentStarted,
    FireTrigger,
    Notice,
    ReviewStatus,
    Snapshot,
    TaskSpec,
    TaskState,
    TaskStatus,
)
from tests.agent_fakes import (
    ROLES,
    SHIPPED_TRIGGERS,
    FakeTree,
    Script,
    board,
    claims_done,
    files,
    reports,
    send,
    session_for,
    settle,
)
from tests.session_builders import last_of, messages, opened, saved, store_with, switching

SUPER_TASKS = [(role, SUPER_REVIEW, None, "completed") for role in SUPER_LENS_REVIEWERS]
"""The three lens tasks a super review queues, as they end."""

ONE_ROUND = ReviewLimits(rounds=1)
"""Review limits pausing review after round 1, so a super review's batch isn't reviewed."""


def shipped(*scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any) -> WorkspaceHost:
    """A session with the shipped roles and triggers, reviewing what lands in a tree, its tasks
    and lenses played by ``scripts`` and its merge passes by ``reviewers``; ``options`` go to
    :func:`session_for`."""
    session, _, _ = session_for(
        *scripts,
        reviewers=reviewers,
        tree=FakeTree(changes=("src/x.py",)),
        review=True,
        triggers=SHIPPED_TRIGGERS,
        **options,
    )
    return session


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str | None, TaskStatus]]:
    """Each of ``tasks``' role, origin, batch and status."""
    return [(t.task.role, t.origin, t.batch, t.status) for t in tasks]


async def super_reviewed(session: WorkspaceHost) -> tuple[TaskState, ...]:
    """The board once the super review fired by hand in ``session``, and all it led to, have
    run; the session is closed after."""
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    return board(events)


async def test_ctrl_g_queues_the_three_lenses_whose_findings_one_merge_pass_files() -> None:
    session = shipped(
        reports(("Naming drifted", "normal")),
        reports(("Two copies of the parser", "normal")),
        claims_done,
        claims_done,  # The task the merge pass files.
        reviewers=(files("Share the parser", ["F2"], ["F1"]),),
        review_limits=ONE_ROUND,
    )
    tasks = await super_reviewed(session)

    assert shown(tasks) == [
        *SUPER_TASKS,
        (REVIEWER, "super-merge-pass", None, "completed"),
        (CODER, "super-merge-pass", "batch-1", "completed"),
    ]
    assert {t.queued_as for t in tasks[:3]} == {"review"}
    assert len({t.task.prompt for t in tasks[:3]}) == 1  # One rendering, one prompt.
    merge = tasks[3].task.prompt
    assert merge.startswith("Merge the findings of the super review at base")
    assert "You may file at most 10 tasks." in merge
    assert f"F1 · {STYLE_REVIEWER} · normal: Naming drifted" in merge
    assert f"F2 · {DRIFT_REVIEWER} · normal: Two copies of the parser" in merge


async def test_the_merge_pass_shows_under_super_review_but_the_coder_it_files_doesnt() -> None:
    session = shipped(
        reports(("Naming drifted", "normal")),
        claims_done,
        claims_done,
        claims_done,  # The task the merge pass files.
        reviewers=(files("Rename it", ["F1"], []),),
        review_limits=ONE_ROUND,
    )
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    tabs = [(e.role, e.tab) for e in events if isinstance(e, AgentStarted)]
    assert tabs[3:] == [(REVIEWER, "super-review"), (CODER, None)]


async def test_with_the_drift_reviewer_disabled_a_super_review_queues_two_tasks() -> None:
    roles = {**ROLES, DRIFT_REVIEWER: ROLES[DRIFT_REVIEWER].model_copy(update={"enabled": False})}
    session = shipped(claims_done, claims_done, roles=roles)
    tasks = await super_reviewed(session)

    assert [t.task.role for t in tasks] == [STYLE_REVIEWER, SCALING_REVIEWER]


async def test_a_batch_filed_from_a_drift_finding_is_reviewed_by_drift_bugs_and_tests() -> None:
    session = shipped(
        claims_done,
        reports(("Two copies of the parser", "normal")),
        claims_done,
        claims_done,  # The task the merge pass files.
        claims_done,  # Its batch's review: Bugs,
        claims_done,  # Tests,
        claims_done,  # and Drift.
        reviewers=(files("Share the parser", ["F1"], []),),
    )
    tasks = await super_reviewed(session)

    assert shown(tasks) == [
        *SUPER_TASKS,
        (REVIEWER, "super-merge-pass", None, "completed"),
        (CODER, "super-merge-pass", "batch-1", "completed"),
        (BUGS_REVIEWER, "bugs-review", None, "completed"),
        (TESTS_REVIEWER, "tests-review", None, "completed"),
        (DRIFT_REVIEWER, "super-fix-review", None, "completed"),
    ]
    fix_review = tasks[-1].task.prompt
    assert fix_review.startswith("Review the 1 commits made for review batch-1")


# A session saved waiting for the super-reviewer


def saved_with_a_super_reviewer_waiting() -> SessionRecord:
    """A session as an orbit with the ``super-reviewer`` saved it: one of the user's tasks and a
    super review queued for the ``super-reviewer`` and still to start, booked into a batch still
    filling."""
    reviews = LaneSnapshot(
        planner=PlannerSnapshot(next_batch=2, batches={"batch-1": BatchSnapshot(round=1, cap=10)}),
        jobs={"2": ReviewJob(commits=("5555555abc",), round=0, batch=None, whole=True)},
        batches={"2": "batch-1"},
    )
    coder = TaskState(id="1", task=TaskSpec(prompt="Make it faster."), status="completed")
    review = TaskState(
        id="2",
        task=TaskSpec(prompt="Super review of the codebase at 5555555", role="super-reviewer"),
        status="pending",
        origin=SUPER_REVIEW,
        queued_as="review",
    )
    return saved("old", coder, review, reviews=reviews)


async def test_a_super_reviewer_task_waiting_in_a_saved_session_is_cancelled_with_a_notice() -> (
    None
):
    record = parse_record(record_json(saved_with_a_super_reviewer_waiting()))
    assert isinstance(record, SessionRecord)
    store = store_with(record)
    session, _ = switching(store)
    events = await opened(session, ActivateSession(session="old"))
    await session.close()

    tasks = last_of(Snapshot, events).tasks
    assert [(t.id, t.status, t.reason) for t in tasks] == [
        ("1", "completed", ""),
        ("2", "cancelled", QUEUED_FOR_RETIRED_ROLE),
    ]
    assert messages(Notice, events)[1:] == [
        "Reviews queued before lens reviewers were cancelled; unreviewed: 5555555.",
    ]
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=0)
