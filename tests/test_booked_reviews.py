"""Orbit's own reviews told by their booking in the review lane rather than by their role: a
task a ``commits`` trigger queued, of any role, can't be edited and holds back
``suggestions-pending`` while it waits, and a ``reviewer``'s task from a trigger that books no
review, a draft included, can be edited and holds back nothing; in a session as it runs, and in
one read back from a save. Such a task of another role ends its batch as it ends, however it
does, in the work lane or the review lane, as a review does, unless it's left to run again."""

import asyncio
from collections.abc import Callable

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.ports import Reply
from orbit.agent.domain.reviews import BatchSnapshot, LaneSnapshot, PlannerSnapshot, ReviewJob
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SessionRecord, parse_record, record_json
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    INTERNAL,
    INVALID,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    ActivateSession,
    CancelTurn,
    CommandRefused,
    EditTask,
    ErrorOccurred,
    Event,
    ReviewStatus,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TaskState,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
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


# A booked task of another role ending


AUDITOR = "auditor"

AUDITOR_IN = {
    lane: {
        **ROLES,
        AUDITOR: Role(
            instructions="Audit the code.",
            verify=False,
            lane=lane,
            workspace="snapshot",
            tools=ROLES[REVIEWER].tools,
        ),
    }
    for lane in ("work", "review")
}
"""The built-in roles, with an ``auditor``, which may suggest tasks as a reviewer may, working
in a snapshot, in the work lane or in the review lane, by lane."""

AUDITOR_AUDITS = AUDITS.model_copy(update={"role": AUDITOR})
"""A project's ``commits`` trigger queuing an auditor's audit after each of the user's
commits."""

LANES = pytest.mark.parametrize("lane", ["work", "review"])
"""Each lane the auditor may be in."""

LIMIT = "You've hit your session limit · resets 6pm (UTC)"
"""Claude Code's words for its usage limit."""


def auditing(
    lane: str,
    *scripts: Script,
    reviewers: tuple[Script, ...] = (),
    errors: list[str] | None = None,
    **triggers: Trigger,
) -> WorkspaceHost:
    """A session auditing each of the user's commits with an auditor in ``lane``, its tasks
    played by ``scripts`` and its reviews by ``reviewers``, the error each turn reports taken
    from ``errors``, if given, with the ``triggers`` besides the audit; a usage limit's pause in
    it is over at once."""
    session, _, launcher = session_for(
        *scripts,
        reviewers=reviewers,
        tree=FakeTree(),
        review=True,
        roles=AUDITOR_IN[lane],
        triggers={AUDIT: AUDITOR_AUDITS, **triggers},
        pause=LimitPause(sleep=no_wait),
    )
    if errors is not None:
        launcher.turn_errors = errors
    return session


async def no_wait(seconds: float) -> None:
    """Waits no time at all."""


def open_batches(events: list[Event]) -> list[int]:
    """The batches each review status in ``events`` says are open, in order."""
    return [event.open_batches for event in events if isinstance(event, ReviewStatus)]


def titled(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str | None, str]]:
    """Each of ``tasks``' role, origin, batch and title."""
    return [(t.task.role, t.origin, t.batch, t.task.prompt.split("\n")[0]) for t in tasks]


@LANES
async def test_a_booked_task_of_another_role_has_its_suggestions_reviewed_once_it_ends(
    lane: str,
) -> None:
    session = auditing(
        lane,
        claims_done,
        suggests("Add tests"),
        claims_done,
        reviewers=(claims_done, claims_done),
        review=TRIGGERS["review"],
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    # The user's commit is reviewed on its own (batch-1), and audited (batch-2).
    assert titled(board(events)) == [
        (CODER, "manual", None, "Make it faster."),
        (REVIEWER, "review", None, "Review commit c1"),
        (AUDITOR, AUDIT, None, "Audit at c1."),
        (CODER, AUDIT, "batch-2", "Add tests"),
        (REVIEWER, "review", None, "Review the 1 commits made for review batch-2"),
    ]
    assert open_batches(events)[-1] == 0


@pytest.mark.parametrize(
    ("lane", "trigger"),
    [("work", AUDITS), ("work", AUDITOR_AUDITS), ("review", AUDITOR_AUDITS)],
    ids=["coder landing", "auditor in the work lane", "auditor in the review lane"],
)
async def test_a_booked_task_of_another_role_suggesting_nothing_closes_its_batch(
    lane: str, trigger: Trigger
) -> None:
    session = auditing(lane, claims_done, claims_done, audit=trigger)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(t.origin, t.status) for t in board(events)] == [
        ("manual", "completed"),
        (AUDIT, "completed"),
    ]
    assert open_batches(events)[-2:] == [1, 0]


def reports(error: str, errors: list[str]) -> Script:
    """An agent whose turn reports ``error``, put among ``errors``, the turn errors its launcher
    reports, as it ends in error."""

    async def errs(call: Call) -> TurnEndReason:
        errors.append(error)
        return "error"

    return errs


@LANES
async def test_a_booked_task_of_another_role_failing_closes_its_batch(lane: str) -> None:
    errors: list[str] = []
    session = auditing(lane, claims_done, reports("Agent crashed", errors), errors=errors)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [t.status for t in board(events)] == ["completed", "error"]
    assert open_batches(events)[-2:] == [1, 0]


def hangs_once_started(started: asyncio.Event) -> Script:
    """An agent that fetches its task, sets ``started``, and works until stopped."""

    async def works(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return works


@LANES
async def test_a_booked_task_of_another_role_stopped_by_you_closes_its_batch(lane: str) -> None:
    started = asyncio.Event()
    session = auditing(lane, claims_done, hangs_once_started(started))
    await start(session)
    await asyncio.wait_for(started.wait(), 10)
    await send(session, CancelTurn(lane=lane))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [t.status for t in board(events)] == ["completed", "cancelled"]
    assert open_batches(events)[-2:] == [1, 0]


def all_completed(count: int) -> Callable[[list[Event]], bool]:
    """Stops once the last board in the events has ``count`` tasks, all completed."""
    return lambda events: [t.status for t in latest_board(events)] == ["completed"] * count


@LANES
async def test_a_booked_task_of_another_role_left_to_run_again_suggests_into_its_batch_after(
    lane: str,
) -> None:
    errors: list[str] = []
    scripts = (claims_done, reports(LIMIT, errors), suggests("Add tests"), claims_done)
    session = auditing(lane, *scripts, errors=errors)
    await start(session)
    events = await asyncio.wait_for(settle(session, until=all_completed(3)), 10)
    await session.close()

    assert titled(board(events)) == [
        (CODER, "manual", None, "Make it faster."),
        (AUDITOR, AUDIT, None, "Audit at c1."),
        (CODER, AUDIT, "batch-1", "Add tests"),
    ]
    assert open_batches(events)[-1] == 0


async def test_a_booked_task_the_work_lane_runs_again_once_it_failed_ends_its_batch_once() -> None:
    errors: list[str] = []
    replies: list[Reply] = []
    failing = reports("Agent crashed", errors)
    auditor = suggests("Add tests", replies=replies)
    session = auditing("work", claims_done, failing, auditor, errors=errors)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await send(session, RunTasks())
    ran_again = all_completed(2)
    events = await asyncio.wait_for(settle(session, events, until=ran_again), 10)
    await session.close()

    assert [reply.text for reply in replies] == [
        "This review's suggestions are closed: its batch is over."
    ]
    assert INTERNAL not in [event.code for event in events if isinstance(event, ErrorOccurred)]
    assert open_batches(events)[-1] == 0


async def test_a_reviewers_review_still_ends_its_batch_once() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"), claims_done),
        tree=FakeTree(),
        review=True,
        triggers={"review": TRIGGERS["review"]},
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert titled(board(events)) == [
        (CODER, "manual", None, "Make it faster."),
        (REVIEWER, "review", None, "Review commit c1"),
        (CODER, "review", "batch-1", "Add tests"),
        (REVIEWER, "review", None, "Review the 1 commits made for review batch-1"),
    ]
    assert open_batches(events)[-1] == 0


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
