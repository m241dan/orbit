"""Orbit's own reviews told by their booking in the review lane rather than by their role: a
task a ``commits`` trigger queued, of any role, can't be edited and holds back
``suggestions-pending`` while it waits, and a ``reviewer``'s task from a trigger that books no
review, a draft included, can be edited and holds back nothing; in a session as it runs, and in
one read back from a save."""

import asyncio

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.sessions import SessionRecord, parse_record, record_json
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    INVALID,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    ActivateSession,
    CommandRefused,
    EditTask,
    Event,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
)
from tests.agent_fakes import (
    TRIGGERS,
    Call,
    FakeTree,
    Script,
    board,
    claims_done,
    refused,
    send,
    session_for,
    settle,
    start,
    suggests,
    super_every,
)
from tests.session_builders import opened, saved, store_with
from tests.waiting import eventually

AUDIT = "audit"
LOOK = "look"

AUDITS = Trigger(
    on="commits", role=CODER, prompt="Audit at {commit}.", every=1, when={"origin": "manual"}
)
"""A project's ``commits`` trigger queuing a coder's audit after each of the user's commits,
booked as a review of the whole codebase."""


def looks(draft: bool = False) -> Trigger:
    """A project's trigger queuing a reviewer's look after each coder task finishes, unless one
    is still to run, booking no review; a draft if ``draft``."""
    return Trigger(
        on="task_finished",
        role=REVIEWER,
        prompt="Look around.",
        when={"role": CODER},
        guards=("not-queued",),
        draft=draft,
    )


def triggers_with(super_reviews: int, **own: Trigger) -> dict[str, Trigger]:
    """The shipped triggers without the review, the super review due every ``super_reviews`` of
    the user's commits, with the project's ``own``."""
    off = TRIGGERS["review"].model_copy(update={"enabled": False})
    return {**super_every(super_reviews), "review": off, **own}


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str]]:
    """Each of ``tasks``' role, origin and what it was queued as."""
    return [(state.task.role, state.origin, state.queued_as) for state in tasks]


def gated(gate: asyncio.Event) -> Script:
    """A reviewer that claims done once ``gate`` opens."""

    async def review(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Looked")
        return "completed"

    return review


def refusals(events: list[Event]) -> list[CommandRefused]:
    return [event for event in events if isinstance(event, CommandRefused)]


def any_refused(events: list[Event]) -> bool:
    """Whether a command was refused in ``events``."""
    return bool(refusals(events))


def latest_board(events: list[Event]) -> tuple[TaskState, ...]:
    """The tasks last published in ``events``, or none if none were."""
    return board(events) if any(isinstance(e, TasksChanged) for e in events) else ()


def super_reviews(events: list[Event]) -> bool:
    """Whether the last board in ``events`` has a super review on it."""
    return any(state.task.role == SUPER_REVIEWER for state in latest_board(events))


async def super_review_queued(
    session: WorkspaceHost, gate: asyncio.Event, events: list[Event]
) -> list[Event]:
    """``events``, and those of ``session`` until it's done, once a super review is queued while
    the reviewer held by ``gate`` looks, which ``gate`` then lets finish."""
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: super_reviews(events))
    gate.set()
    return await asyncio.wait_for(collecting, 10)


# In a session as it runs


async def test_a_commits_triggers_task_of_another_role_holds_back_suggestions_pending() -> None:
    session, _, _ = session_for(
        *[claims_done] * 4, tree=FakeTree(), review=True, triggers=triggers_with(2, audit=AUDITS)
    )
    await start(session, "One.", "Two.")
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    # The second commit makes the super review due, but the audit queued on the first waits.
    assert shown(board(events)) == [
        (CODER, "manual", "task"),
        (CODER, "manual", "task"),
        (CODER, AUDIT, "review"),
        (CODER, AUDIT, "review"),
    ]


async def test_a_commits_triggers_task_of_another_role_cant_be_edited() -> None:
    session, _, _ = session_for(
        claims_done, claims_done, tree=FakeTree(), review=True, triggers={AUDIT: AUDITS}
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await send(session, EditTask(task_id="2", task=TaskSpec(prompt="Audit again.")))
    events = await asyncio.wait_for(settle(session, events), 10)
    await session.close()

    assert refused(INVALID, "Reviews can't be edited; remove it instead.") in events
    [_, audit] = board(events)
    assert (audit.origin, audit.task.prompt) == (AUDIT, "Audit at c1.")


async def test_a_reviewer_task_from_a_trigger_booking_no_review_holds_back_nothing() -> None:
    gate = asyncio.Event()
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(gated(gate), suggests()),
        tree=FakeTree(),
        review=True,
        triggers=triggers_with(2, look=looks()),
    )
    await start(session, "One.", "Two.")
    events = await super_review_queued(session, gate, [])
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", "task"),
        (CODER, "manual", "task"),
        (REVIEWER, LOOK, "task"),
        (SUPER_REVIEWER, SUPER_REVIEW, "review"),
    ]


def draft_waits(events: list[Event]) -> bool:
    """Whether the last board in ``events`` has the look's draft after the finished task."""
    return [state.status for state in latest_board(events)] == ["completed", "draft"]


@pytest.mark.parametrize("draft", [False, True])
async def test_a_reviewer_task_from_a_trigger_booking_no_review_can_be_edited(
    draft: bool,
) -> None:
    session, _, _ = session_for(
        claims_done,
        reviewers=(claims_done, claims_done),
        tree=FakeTree(),
        review=True,
        triggers={LOOK: looks(draft)},
    )
    await start(session)
    events = await asyncio.wait_for(settle(session, until=draft_waits if draft else None), 10)
    assert [state.status for state in board(events)][1] == ("draft" if draft else "completed")
    closer = TaskSpec(prompt="Look closer.", role=REVIEWER)
    await send(session, EditTask(task_id="2", task=closer))
    events = await asyncio.wait_for(settle(session, events), 10)
    await session.close()

    assert refusals(events) == []
    [_, look] = board(events)
    assert (look.origin, look.queued_as, look.task.prompt) == (LOOK, "task", "Look closer.")


# In a session read back from a save


AUDITED = (
    TaskState(id="1", task=TaskSpec(prompt="One.")),
    TaskState(id="2", task=TaskSpec(prompt="Audit at c0."), origin=AUDIT, queued_as="review"),
)
"""A board saved with the user's task to run, then the audit of the codebase at ``c0``."""

AUDITING = LaneSnapshot(
    planner=PlannerSnapshot(next_batch=2, batches={"batch-1": BatchSnapshot(round=1, cap=10)}),
    jobs={"2": ReviewJob(commits=("c0",), round=1, batch=None, whole=True)},
    batches={"2": "batch-1"},
)
"""The review lane of :data:`AUDITED`, the audit booked as a review of the whole codebase."""

LOOKING = (
    TaskState(id="1", task=TaskSpec(prompt="One.")),
    TaskState(id="2", task=TaskSpec(prompt="Look around.", role=REVIEWER), origin=LOOK),
    TaskState(
        id="3", task=TaskSpec(prompt="Look again.", role=REVIEWER), status="draft", origin=LOOK
    ),
)
"""A board saved with the user's task to run, then a look, and a look's draft, from a trigger
that books no review."""


def read_back(*tasks: TaskState, reviews: LaneSnapshot | None = None) -> SessionRecord:
    """The session ``auth``, saved with ``tasks`` and ``reviews``, as it's read back."""
    record = parse_record(record_json(saved("auth", *tasks, reviews=reviews)))
    assert isinstance(record, SessionRecord)
    return record


async def opening(
    record: SessionRecord, *scripts: Script, reviewers: tuple[Script, ...] = ()
) -> tuple[WorkspaceHost, list[Event]]:
    """A session that opened ``record``, its super review due on each of the user's commits,
    its tasks played by ``scripts`` and its reviews by ``reviewers``, and the events from
    opening it."""
    session, _, _ = session_for(
        *scripts,
        reviewers=reviewers,
        tree=FakeTree(),
        review=True,
        triggers=triggers_with(1),
        store=store_with(record),
    )
    return session, await opened(session, ActivateSession(session="auth"))


async def test_a_saved_review_of_another_role_still_holds_back_suggestions_pending() -> None:
    record = read_back(*AUDITED, reviews=AUDITING)
    session, events = await opening(record, claims_done, claims_done)
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    await session.close()

    # The user's commit makes the super review due, but the audit saved still to run waits.
    assert shown(board(events)) == [(CODER, "manual", "task"), (CODER, AUDIT, "review")]


async def test_a_saved_review_of_another_role_still_cant_be_edited() -> None:
    session, events = await opening(read_back(*AUDITED, reviews=AUDITING))
    await send(session, EditTask(task_id="2", task=TaskSpec(prompt="Audit again.")))
    events = await asyncio.wait_for(settle(session, events, until=any_refused), 10)
    await session.close()

    assert refused(INVALID, "Reviews can't be edited; remove it instead.") in events


async def test_a_saved_reviewer_task_booking_no_review_still_holds_back_nothing() -> None:
    gate = asyncio.Event()
    record = read_back(*LOOKING[:2])
    session, events = await opening(record, claims_done, reviewers=(gated(gate), suggests()))
    await send(session, RunTasks())
    events = await super_review_queued(session, gate, events)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", "task"),
        (REVIEWER, LOOK, "task"),
        (SUPER_REVIEWER, SUPER_REVIEW, "review"),
    ]


CLOSER = TaskSpec(prompt="Look closer.", role=REVIEWER)


def looks_closer(events: list[Event]) -> bool:
    """Whether the last board in ``events`` has a task edited to :data:`CLOSER`."""
    return any(state.task == CLOSER for state in latest_board(events))


@pytest.mark.parametrize("task_id", ["2", "3"])
async def test_a_saved_reviewer_task_booking_no_review_can_still_be_edited(task_id: str) -> None:
    looking = (claims_done, claims_done)  # The look, and the look edited, may start at once.
    session, events = await opening(read_back(*LOOKING), reviewers=looking)
    await send(session, EditTask(task_id=task_id, task=CLOSER))
    events = await asyncio.wait_for(settle(session, events, until=looks_closer), 10)
    await session.close()

    assert refusals(events) == []
    assert board(events)[int(task_id) - 1].task == CLOSER
