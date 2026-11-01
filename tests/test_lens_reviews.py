"""Reviews by lens in a session: the tasks the triggers ``review-rounds`` guards queue for one
landing booked into one review, the findings its lenses report with ``report_finding``, and
``reviews_finished`` raised once they have all ended, however they did, its tasks filing into
the review's batch; in a session as it runs, and in one read back from a save."""

import asyncio
from typing import Any

from orbit.agent import HostParts, WorkspaceHost
from orbit.agent.application.ports import Reply
from orbit.agent.domain.reviews import (
    BatchSnapshot,
    Finding,
    LaneSnapshot,
    PlannerSnapshot,
    ReviewGroup,
    ReviewJob,
    ReviewLimits,
)
from orbit.agent.domain.roles import Role
from orbit.agent.domain.tools import (
    COMPLETE_TASK,
    GET_TASK,
    GIVE_UP,
    REPORT_FINDING,
    REPORT_PROGRESS,
)
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    REVIEWER,
    ActivateSession,
    AddTask,
    Event,
    RemoveTask,
    ReviewStatus,
    RunTasks,
    Snapshot,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    ROLES,
    TRIGGERS,
    Call,
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    Ticks,
    agents_for,
    board,
    claims_done,
    gives_up,
    send,
    session_for,
    settle,
    start,
    suggests,
)
from tests.session_builders import last_of, opened, saved, store_with
from tests.waiting import eventually

BUGS = "bugs-reviewer"
TESTS = "tests-reviewer"

LENS_TOOLS = (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP, REPORT_FINDING)


def lens(blocking: bool) -> Role:
    """A lens role in the review lane, reporting findings, blocking ones if ``blocking``."""
    return Role(
        instructions="Report what you find.",
        verify=False,
        lane="review",
        workspace="snapshot",
        tools=LENS_TOOLS,
        blocking_findings=blocking,
    )


LENS_ROLES = {**ROLES, BUGS: lens(blocking=True), TESTS: lens(blocking=False)}
"""The built-in roles, with a bugs lens, which may report blocking findings, and a tests lens,
which may not."""


def lens_trigger(role: str) -> Trigger:
    """A project's trigger queuing ``role``'s look at each commit ``review-rounds`` finds due."""
    return Trigger(
        on="task_landed",
        role=role,
        prompt=f"Look at {{commit}} as {role}.",
        guards=("review-rounds",),
    )


MERGE = Trigger(
    on="reviews_finished",
    role=REVIEWER,
    prompt="{title}\n\n{findings}\n\nAt most {cap}, a {trigger}.",
    when={"findings": "some"},
)
"""A project's merge pass, on a review whose lenses found something."""

QUIET = Trigger(
    on="reviews_finished",
    role=REVIEWER,
    prompt="Nothing found at {commit}.",
    when={"findings": "none"},
)
"""A project's trigger on a review whose lenses found nothing."""


def lens_triggers(**more: Trigger) -> dict[str, Trigger]:
    """The shipped triggers with the review disabled, a bugs and a tests lens reviewing each
    commit, and ``more``."""
    off = TRIGGERS["review"].model_copy(update={"enabled": False})
    return {
        **TRIGGERS,
        "review": off,
        "bugs": lens_trigger(BUGS),
        "tests": lens_trigger(TESTS),
    } | more


def reports(*findings: tuple[str, str], replies: list[Reply] | None = None) -> Script:
    """A lens that reports ``findings``, each a title and severity, recording orbit's replies
    in ``replies``, if given, then claims done."""

    async def look(call: Call) -> TurnEndReason:
        await call("get_task")
        for title, severity in findings:
            body = f"Details of {title.lower()}."
            reply = await call("report_finding", title=title, body=body, severity=severity)
            if replies is not None:
                replies.append(reply)
        await call("complete_task", summary="Looked")
        return "completed"

    return look


def lensing(*scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any) -> WorkspaceHost:
    """A session landing work in a tree and reviewing it by lens, with the triggers of
    :func:`lens_triggers` unless ``options`` give others, its tasks played by ``scripts`` and
    its reviews by ``reviewers``; the other ``options`` go to :func:`session_for`."""
    defaults: dict[str, Any] = {"roles": LENS_ROLES, "triggers": lens_triggers()}
    session, _, _ = session_for(
        *scripts, reviewers=reviewers, tree=FakeTree(), review=True, **(defaults | options)
    )
    return session


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str | None, TaskStatus]]:
    """Each of ``tasks``' role, origin, batch and status."""
    return [(t.task.role, t.origin, t.batch, t.status) for t in tasks]


def boards(events: list[Event]) -> list[tuple[TaskState, ...]]:
    """Each board published in ``events``, in order."""
    return [event.tasks for event in events if isinstance(event, TasksChanged)]


def reviews_of(store: FakeStore) -> LaneSnapshot:
    """The review state last saved in ``store``."""
    reviews = store.last.reviews
    assert reviews is not None
    return reviews


TWO_FINDINGS = (
    "Merge the findings of the review of commit c1\n\n"
    "F1 · bugs-reviewer · blocking: Crash on empty input\nDetails of crash on empty input.\n\n"
    "F2 · tests-reviewer · normal: No test for it\nDetails of no test for it.\n\n"
    "At most 5, a merge."
)
"""The merge pass's prompt for a review of c1 whose bugs and tests lenses each found one
thing."""


# In a session as it runs


async def test_a_landing_books_both_lenses_into_one_review_that_fans_in_once() -> None:
    store = FakeStore()
    session = lensing(
        claims_done,
        reports(("Crash on empty input", "blocking")),
        reports(("No test for it", "normal")),
        reviewers=(claims_done,),
        triggers=lens_triggers(merge=MERGE),
        store=store,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        (BUGS, "bugs", None, "completed"),
        (TESTS, "tests", None, "completed"),
        (REVIEWER, "merge", None, "completed"),
    ]
    assert board(events)[3].task.prompt == TWO_FINDINGS
    # The merge pass was queued once, as the second lens ended.
    first = next(tasks for tasks in boards(events) if len(tasks) == 4)
    assert [t.status for t in first[1:3]] == ["completed", "completed"]
    assert all(len(tasks) <= 4 for tasks in boards(events))
    reviews = reviews_of(store)
    assert reviews.batches == {"2": "batch-1", "3": "batch-1", "4": "batch-1"}
    assert (reviews.groups, reviews.planner.batches) == ({}, {})


async def test_the_merge_pass_files_into_the_reviews_batch_under_its_one_cap() -> None:
    replies: list[Reply] = []
    session = lensing(
        claims_done,
        reports(("Crash on empty input", "blocking")),
        reports(),
        claims_done,  # The suggestion.
        reports(),  # The batch's review, by both lenses.
        reports(),
        reviewers=(suggests("Fix the crash", "Add a test", replies=replies),),
        triggers=lens_triggers(merge=MERGE),
        review_limits=ReviewLimits(suggestions=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies] == [
        "Queued as task 5.",
        "This review has already suggested 1 tasks, the most allowed.",
    ]
    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        (BUGS, "bugs", None, "completed"),
        (TESTS, "tests", None, "completed"),
        (REVIEWER, "merge", None, "completed"),
        (CODER, "merge", "batch-1", "completed"),
        (BUGS, "bugs", None, "completed"),
        (TESTS, "tests", None, "completed"),
    ]
    statuses = [event for event in events if isinstance(event, ReviewStatus)]
    assert statuses[-1] == ReviewStatus(paused=False, open_batches=0)


async def test_a_review_closes_its_batch_once_the_merge_pass_ends() -> None:
    session = lensing(
        claims_done,
        reports(("No test for it", "normal")),
        reports(),
        reviewers=(claims_done,),
        triggers=lens_triggers(merge=MERGE),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    opened_batches = [e.open_batches for e in events if isinstance(e, ReviewStatus)]
    assert opened_batches == [1, 0]


async def test_lenses_that_report_nothing_or_give_up_fan_in_with_no_findings() -> None:
    session = lensing(
        claims_done,
        reports(),
        gives_up,
        reviewers=(claims_done,),
        triggers=lens_triggers(merge=MERGE, quiet=QUIET),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        (BUGS, "bugs", None, "completed"),
        (TESTS, "tests", None, "gave_up"),
        (REVIEWER, "quiet", None, "completed"),
    ]
    assert board(events)[3].task.prompt == "Nothing found at c1."


async def test_a_lens_taken_off_the_board_counts_as_ended() -> None:
    gate = asyncio.Event()

    async def held(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("report_finding", title="Off by one", body="", severity="normal")
        await gate.wait()
        await call("complete_task", summary="Looked")
        return "completed"

    session = lensing(
        claims_done, held, reviewers=(claims_done,), triggers=lens_triggers(merge=MERGE)
    )
    await start(session)
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: any(len(tasks) == 3 for tasks in boards(events)))
    await send(session, RemoveTask(task_id="3"))
    await eventually(lambda: [t.id for t in boards(events)[-1]] == ["1", "2"])
    gate.set()
    await asyncio.wait_for(collecting, 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        (BUGS, "bugs", None, "completed"),
        (REVIEWER, "merge", None, "completed"),
    ]
    assert (
        board(events)[2].task.prompt.split("\n\n")[1] == "F1 · bugs-reviewer · normal: Off by one"
    )


# Refusals


async def test_a_blocking_finding_from_a_lens_without_blocking_findings_is_refused() -> None:
    replies: list[Reply] = []
    session = lensing(
        claims_done,
        reports(),
        reports(
            ("Crash on empty input", "blocking"), ("No test for it", "normal"), replies=replies
        ),
    )
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(reply.text, reply.is_error) for reply in replies] == [
        (
            "Only a role with blocking_findings = true may report a blocking finding; report it"
            " as normal.",
            True,
        ),
        ("Recorded as F1.", False),
    ]


async def test_a_finding_past_lens_findings_is_refused() -> None:
    replies: list[Reply] = []
    titles = [(f"Finding {n}", "normal") for n in range(1, 12)]
    session = lensing(claims_done, reports(*titles, replies=replies), reports())
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies[:10]] == [f"Recorded as F{n}." for n in range(1, 11)]
    assert (replies[10].text, replies[10].is_error) == (
        "This review has already reported 10 findings, the most allowed.",
        True,
    )


async def test_a_finding_from_a_task_booked_into_no_review_is_refused() -> None:
    replies: list[Reply] = []
    session = lensing(reports(("Off by one", "normal"), replies=replies))
    await send(session, AddTask(task=TaskSpec(prompt="Look around.", role=BUGS)))
    await send(session, RunTasks())
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(reply.text, reply.is_error) for reply in replies] == [
        ("Only reviews orbit queued itself can report findings.", True)
    ]


async def test_a_finding_is_refused_while_reviews_are_off() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        reports(("Off by one", "normal"), replies=replies), roles=LENS_ROLES
    )
    await send(session, AddTask(task=TaskSpec(prompt="Look around.", role=BUGS)))
    await send(session, RunTasks())
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies] == ["Orbit isn't taking findings."]


async def test_a_finding_of_an_unknown_severity_is_refused() -> None:
    replies: list[Reply] = []
    session = lensing(claims_done, reports(("Off by one", "urgent"), replies=replies), reports())
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies] == [
        'A finding\'s severity is "blocking" or "normal", not \'urgent\'.'
    ]


# In a session read back from a save

JOB = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))
FOUND = Finding(
    id="F1",
    role=BUGS,
    title="Crash on empty input",
    body="Details of crash on empty input.",
    severity="blocking",
)


def lens_task(task_id: str, role: str, status: TaskStatus) -> TaskState:
    """Task ``task_id``, ``role``'s look queued as a review by its lens trigger, ``status``."""
    return TaskState(
        id=task_id,
        task=TaskSpec(prompt=f"Look at c1 as {role}.", role=role),
        status=status,
        origin="bugs" if role == BUGS else "tests",
        queued_as="review",
    )


def mid_review(tests_status: TaskStatus) -> tuple[tuple[TaskState, ...], LaneSnapshot]:
    """A board and review lane saved as the review of task 1's commit went on: its bugs lens
    ended having found :data:`FOUND`, and its tests lens is ``tests_status``."""
    done = TaskState(id="1", task=TaskSpec(prompt="Make it faster."), status="completed")
    tasks = (done, lens_task("2", BUGS, "completed"), lens_task("3", TESTS, tests_status))
    lane = LaneSnapshot(
        planner=PlannerSnapshot(next_batch=2, batches={"batch-1": BatchSnapshot(round=1, cap=5)}),
        jobs={"2": JOB, "3": JOB},
        batches={"2": "batch-1", "3": "batch-1"},
        groups={"batch-1": ReviewGroup(JOB, lenses=("2", "3"), ended=("2",), findings=(FOUND,))},
    )
    return tasks, lane


def reopening(
    store: FakeStore, *scripts: Script, triggers: dict[str, Trigger]
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session saved in ``store`` reviewing by lens with ``triggers``, whose tasks run
    through agents playing ``scripts``."""
    agents, launcher = agents_for(
        *scripts, tree=FakeTree(), review=True, roles=LENS_ROLES, triggers=triggers
    )
    host = WorkspaceHost(
        HostParts(agents=agents, store=store, clock=Ticks(), new_id=lambda: "session-1")
    )
    return host, launcher


async def test_a_review_saved_mid_way_fans_in_with_its_findings_once_its_last_lens_ends() -> None:
    tasks, lane = mid_review("pending")
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, launcher = reopening(
        store, reports(("No test for it", "normal")), triggers=lens_triggers(merge=MERGE)
    )
    launcher.reviewers.append(claims_done)
    await opened(session, ActivateSession(session="auth"))
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events))[3:] == [(REVIEWER, "merge", None, "completed")]
    assert board(events)[3].task.prompt == TWO_FINDINGS
    assert reviews_of(store).groups == {}


async def test_a_review_whose_last_lens_was_interrupted_fans_in_on_load() -> None:
    tasks, lane = mid_review("running")
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, launcher = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await asyncio.sleep(0.05)
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert shown(snapshot.tasks)[2:] == [
        (TESTS, "tests", None, "cancelled"),
        (REVIEWER, "merge", None, "pending"),
    ]
    assert snapshot.tasks[3].task.prompt.split("\n\n")[1] == (
        "F1 · bugs-reviewer · blocking: Crash on empty input\nDetails of crash on empty input."
    )
    assert snapshot.review == ReviewStatus(paused=False, open_batches=1)
    assert reviews_of(store).batches["4"] == "batch-1"
    assert launcher.launches == []


async def test_a_review_interrupted_with_no_trigger_on_its_end_closes_on_load() -> None:
    tasks, lane = mid_review("running")
    session, _ = reopening(
        store_with(saved("auth", *tasks, reviews=lane)), triggers=lens_triggers()
    )
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=0)
