"""The per-commit lens reviewers and the merge pass orbit ships: a landing queues the six lens
tasks, whose findings, if any, the ``reviewer`` files into the review's batch; a batch of fixes
reviewed narrowly, by Bugs, Tests and the lenses whose findings it fixes; and the reviews a
session saved before them, cancelled as it opens."""

import asyncio
import json
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.application.ports import Reply, TreeFailed
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.endings import INTERRUPTED, QUEUED_BEFORE_LENSES
from orbit.agent.domain.reviews import (
    BLOCKING_REFUSED,
    BatchSnapshot,
    LaneSnapshot,
    PlannerSnapshot,
    ReviewJob,
    ReviewLimits,
    pause_notice,
)
from orbit.agent.domain.sessions import SessionRecord, parse_record, record_json
from orbit.protocol import (
    BUGS_REVIEWER,
    CODER,
    LENS_REVIEWERS,
    REVIEWER,
    ActivateSession,
    Notice,
    ReviewStatus,
    Snapshot,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    SHIPPED_TRIGGERS,
    Call,
    FakeStore,
    FakeTree,
    Script,
    Steps,
    board,
    claims_done,
    files,
    keeps_claiming,
    reports,
    session_for,
    settle,
    start,
    verified,
)
from tests.session_builders import last_of, messages, opened, saved, store_with, switching

LENS_TRIGGERS = tuple(f"{role.removesuffix('-reviewer')}-review" for role in LENS_REVIEWERS)
"""The shipped lens triggers, each named for its lens, in the order they fire."""


def shipped(*scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any) -> WorkspaceHost:
    """A session with the shipped roles and triggers, reviewing what lands in a tree, its tasks
    and lenses played by ``scripts`` and its merge passes by ``reviewers``; review pauses after
    round 1, so a batch isn't reviewed. ``options`` go to :func:`session_for`."""
    defaults: dict[str, Any] = {
        "triggers": SHIPPED_TRIGGERS,
        "review_limits": ReviewLimits(rounds=1),
        "tree": FakeTree(),
    }
    session, _, _ = session_for(*scripts, reviewers=reviewers, review=True, **(defaults | options))
    return session


LENSES_OFF = {
    name: SHIPPED_TRIGGERS[name].model_copy(update={"enabled": False}) for name in LENS_TRIGGERS
}
"""The shipped lens triggers, disabled, as a project replacing them sets them."""


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str | None, TaskStatus]]:
    """Each of ``tasks``' role, origin, batch and status."""
    return [(t.task.role, t.origin, t.batch, t.status) for t in tasks]


LENS_TASKS = [
    (role, name, None, "completed")
    for role, name in zip(LENS_REVIEWERS, LENS_TRIGGERS, strict=True)
]
"""The six lens tasks a landing queues, as they end."""


async def test_a_landing_queues_the_six_lens_tasks_and_nothing_else_when_they_find_nothing() -> (
    None
):
    lenses = [reports() for _ in LENS_REVIEWERS]
    session = shipped(claims_done, *lenses)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [(CODER, "manual", None, "completed"), *LENS_TASKS]
    assert all(t.queued_as == "review" for t in board(events)[1:])


async def test_a_lens_finding_queues_one_merge_pass_that_files_into_the_reviews_batch() -> None:
    lenses = [
        reports(),
        reports(("Crash on empty input", "blocking")),
        reports(("No test for it", "normal")),
        *(reports() for _ in LENS_REVIEWERS[3:]),
    ]
    session = shipped(
        claims_done,
        *lenses,
        claims_done,  # The task the merge pass files.
        reviewers=(files("Fix the crash", ["F1"], ["F2"]),),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        *LENS_TASKS,
        (REVIEWER, "merge-pass", None, "completed"),
        (CODER, "merge-pass", "batch-1", "completed"),
    ]
    merge = board(events)[7].task.prompt
    assert merge.startswith("Merge the findings of the review of commit ")
    assert "You may file at most 5 tasks." in merge
    assert f"F1 · {BUGS_REVIEWER} · blocking: Crash on empty input" in merge
    assert "F2 · tests-reviewer · normal: No test for it" in merge
    assert board(events)[8].task.prompt.startswith("Fix the crash")


async def merge_prompt(tree: FakeTree, task: str) -> str:
    """The prompt of the merge pass of the review of the commit of ``task``, landed in
    ``tree``, whose Bugs lens reports one finding."""
    lenses = lenses_finding(bugs=("Crash on empty input", "blocking"))
    session = shipped(claims_done, *lenses, reviewers=(dismisses("F1"),), tree=tree)
    await start(session, task)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    merge = board(events)[7]
    assert (merge.task.role, merge.status) == (REVIEWER, "completed")
    return merge.task.prompt


async def test_the_merge_pass_is_given_the_reviewed_commits_message_with_its_plan_line() -> None:
    merge = await merge_prompt(FakeTree(), "Plan: docs/x.md\n\nRead the flag.")
    assert "Reviewed commit c1:\n" in merge
    assert "\n    Plan: docs/x.md\n" in merge
    assert "\n    Read the flag." in merge


async def test_the_merge_pass_runs_without_messages_git_cannot_show() -> None:
    tree = FakeTree()
    tree.failures["messages"] = TreeFailed("git show failed: bad object c1")
    merge = await merge_prompt(tree, "Plan: docs/x.md\n\nRead the flag.")
    assert "Reviewed commit" not in merge and "Plan: docs/x.md" not in merge
    assert "F1 · bugs-reviewer · blocking: Crash on empty input" in merge


async def test_the_bugs_reviewer_may_report_a_blocking_finding_and_the_fit_reviewer_may_not() -> (
    None
):
    fit: list[Reply] = []
    bugs: list[Reply] = []
    lenses = [
        reports(("Missing the flag", "blocking"), replies=fit),
        reports(("Crash on empty input", "blocking"), replies=bugs),
        *(reports() for _ in LENS_REVIEWERS[2:]),
    ]
    session = shipped(claims_done, *lenses, claims_done, reviewers=(files("Fix it", ["F1"], []),))
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(r.text, r.is_error) for r in fit] == [(BLOCKING_REFUSED, True)]
    assert [(r.text, r.is_error) for r in bugs] == [("Recorded as F1.", False)]


# A batch of fixes, reviewed narrowly


def dismisses(*findings: str) -> Script:
    """A merge pass dismissing each of ``findings``."""

    async def merge(call: Call) -> TurnEndReason:
        await call("get_task")
        for finding in findings:
            await call("dismiss_finding", finding=finding, reason="Fixed already.")
        await call("complete_task", summary="Nothing to file")
        return "completed"

    return merge


def watching(store: FakeStore, seen: list[LaneSnapshot], script: Script) -> Script:
    """A task that notes the review state last saved in ``store`` in ``seen`` as it starts,
    then plays ``script``."""

    async def watch(call: Call) -> TurnEndReason:
        reviews = store.last.reviews
        assert reviews is not None
        seen.append(reviews)
        return await script(call)

    return watch


def lenses_finding(**found: tuple[str, str]) -> list[Script]:
    """The six lenses of a commit's review, in order, those named in ``found`` by their lens
    each reporting its one finding, a title and severity, the rest nothing."""
    return [
        reports(found[lens]) if lens in found else reports()
        for lens in (role.removesuffix("-reviewer") for role in LENS_REVIEWERS)
    ]


TWO_ROUNDS = ReviewLimits(rounds=2)
"""Limits under which a batch of round 1 is reviewed, and one of round 2 isn't."""


def batch_lenses(events: list[Any]) -> list[tuple[str, str, str | None, TaskStatus]]:
    """The tasks queued after the first merge pass's, the batch's review and what followed."""
    return shown(board(events))[9:]


async def test_a_batch_fixing_fit_and_bugs_findings_is_reviewed_by_fit_bugs_and_tests() -> None:
    store, seen = FakeStore(), list[LaneSnapshot]()
    session = shipped(
        claims_done,
        *lenses_finding(fit=("Missing the flag", "normal"), bugs=("Crash on empty", "blocking")),
        claims_done,  # The task the merge pass files both findings in.
        watching(store, seen, reports(("Flag still unread", "normal"))),
        reports(),
        reports(),
        reviewers=(files("Fix the flag and crash", ["F1", "F2"], []), dismisses("F1")),
        review_limits=TWO_ROUNDS,
        store=store,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events))[8] == (CODER, "merge-pass", "batch-1", "completed")
    assert batch_lenses(events) == [
        ("fit-reviewer", "fit-review", None, "completed"),
        (BUGS_REVIEWER, "bugs-review", None, "completed"),
        ("tests-reviewer", "tests-review", None, "completed"),
        (REVIEWER, "merge-pass", None, "completed"),
    ]
    assert board(events)[12].task.prompt.startswith(
        "Merge the findings of the review of the 1 commits made for review batch-1"
    )
    # The findings filed into batch-1 were forgotten once its review was queued.
    [during] = seen
    assert list(during.groups) == ["batch-2"]
    assert store.last.reviews is not None
    assert (store.last.reviews.groups, store.last.reviews.severities) == ({}, {})


async def test_a_batch_fixing_only_a_tests_finding_is_reviewed_by_bugs_and_tests() -> None:
    session = shipped(
        claims_done,
        *lenses_finding(tests=("No test for it", "normal")),
        claims_done,
        reports(),
        reports(),
        reviewers=(files("Add a test", ["F1"], []),),
        review_limits=TWO_ROUNDS,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert batch_lenses(events) == [
        (BUGS_REVIEWER, "bugs-review", None, "completed"),
        ("tests-reviewer", "tests-review", None, "completed"),
    ]


async def test_a_fix_in_the_batch_carries_the_findings_of_the_task_it_fixes() -> None:
    session = shipped(
        claims_done,
        *lenses_finding(fit=("Missing the flag", "normal")),
        keeps_claiming,  # The filed task, which fails verification and is fixed.
        claims_done,  # Its fix, which joins the batch.
        reports(),
        reports(),
        reports(),
        reviewers=(files("Read the flag", ["F1"], []),),
        review_limits=TWO_ROUNDS,
        verification=verified(Steps(True, False, False, True, True)),
        limits=Limits(max_attempts=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    fix = board(events)[9]
    assert (fix.batch, fix.fixes) == ("batch-1", board(events)[8].id)
    assert shown(board(events))[10:] == [
        ("fit-reviewer", "fit-review", None, "completed"),
        (BUGS_REVIEWER, "bugs-review", None, "completed"),
        ("tests-reviewer", "tests-review", None, "completed"),
    ]


async def test_a_batch_at_the_last_round_is_not_reviewed_but_pauses_review() -> None:
    session = shipped(
        claims_done,
        *lenses_finding(fit=("Missing the flag", "normal")),
        claims_done,
        reviewers=(files("Read the flag", ["F1"], []),),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert batch_lenses(events) == []
    assert pause_notice(1) in messages(Notice, events)
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].paused


# A session saved before the lens reviewers


def job(commit: str) -> ReviewJob:
    """A review of ``commit`` on its own."""
    return ReviewJob(commits=(commit,), round=0, batch=None, reviewed=("1",))


def review(task_id: str, status: TaskStatus) -> TaskState:
    """Task ``task_id``, a reviewer's review orbit queued, at ``status``."""
    task = TaskSpec(prompt=f"Review commit {task_id}.", role=REVIEWER)
    return TaskState(id=task_id, task=task, status=status, origin="review", queued_as="review")


def saved_before_lenses() -> SessionRecord:
    """A session as an orbit before the lens reviewers saved it, read back now: one of the
    user's tasks, then reviews still to start (2), running (3) and completed (4), each booked
    into a batch of its own; the batches of 2 and 3 still filling."""
    reviews = LaneSnapshot(
        planner=PlannerSnapshot(
            next_batch=4,
            batches={
                "batch-1": BatchSnapshot(round=1, cap=5),
                "batch-2": BatchSnapshot(round=1, cap=5),
            },
        ),
        jobs={"2": job("2222222abc"), "3": job("3333333abc")},
        batches={"2": "batch-1", "3": "batch-2"},
    )
    coder = TaskState(id="1", task=TaskSpec(prompt="Make it faster."), status="completed")
    tasks = (coder, review("2", "pending"), review("3", "running"), review("4", "completed"))
    old = json.loads(record_json(saved("old", *tasks, reviews=reviews)))
    record = parse_record(json.dumps(old | {"version": 25}))
    assert isinstance(record, SessionRecord)
    return record


async def test_a_review_waiting_in_an_older_session_is_cancelled_closing_its_batch() -> None:
    store = store_with(saved_before_lenses())
    session, _ = switching(store)
    events = await opened(session, ActivateSession(session="old"))
    await session.close()

    tasks = last_of(Snapshot, events).tasks
    assert [(t.id, t.status, t.reason) for t in tasks] == [
        ("1", "completed", ""),
        ("2", "cancelled", QUEUED_BEFORE_LENSES),
        ("3", "cancelled", INTERRUPTED),
        ("4", "completed", ""),
    ]
    assert messages(Notice, events)[1:] == [
        "1 task was interrupted and is cancelled.",
        "Reviews queued before lens reviewers were cancelled; unreviewed: 2222222.",
    ]
    assert last_of(Snapshot, events).review == ReviewStatus(paused=False, open_batches=0)
    assert store.last.reviews is not None and store.last.reviews.planner.batches == {}


async def test_an_upgraded_session_opened_again_gives_no_notice_again() -> None:
    store = store_with(saved_before_lenses())
    session, _ = switching(store)
    await opened(session, ActivateSession(session="old"))
    again = store.last.model_copy(update={"id": "again", "name": "again"})
    store.saved.append(again)
    events = await opened(session, ActivateSession(session="again"))
    await session.close()

    [resumed] = messages(Notice, events)
    assert resumed.startswith('Resumed "again"')


async def test_an_older_session_without_reviews_waiting_gives_no_notice() -> None:
    record = saved_before_lenses()
    finished = tuple(
        t.model_copy(update={"status": "completed", "reason": ""}) for t in record.tasks
    )
    store = store_with(record.model_copy(update={"tasks": finished}))
    session, _ = switching(store)
    events = await opened(session, ActivateSession(session="old"))
    await session.close()

    assert messages(Notice, events) == ['Resumed "old" (last active Oct 16 18:02)']


async def test_with_a_project_reviewer_of_its_own_the_landing_queues_it_alone() -> None:
    own = SHIPPED_TRIGGERS[LENS_TRIGGERS[0]].model_copy(
        update={"role": REVIEWER, "prompt": "Review {commit}.\n\n{diff}"}
    )
    session = shipped(
        claims_done,
        reviewers=(claims_done,),
        triggers={**SHIPPED_TRIGGERS, **LENSES_OFF, "mine": own},
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        (REVIEWER, "mine", None, "completed"),
    ]


async def test_with_the_lens_triggers_disabled_and_none_of_its_own_nothing_is_reviewed() -> None:
    session = shipped(claims_done, triggers={**SHIPPED_TRIGGERS, **LENSES_OFF})
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [(CODER, "manual", None, "completed")]
