"""Reviews by lens in a session: the tasks the triggers ``review-rounds`` guards queue for one
landing booked into one review, the findings its lenses report with ``report_finding``, and
``reviews_finished`` raised once they have all ended, however they did, its tasks filing into
the review's batch, the reviews of two landings fanning in apart as they run side by side; in a
session as it runs, and in one read back from a save."""

import asyncio
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent import HostParts, Verification, WorkspaceHost
from orbit.agent.application.ports import Reply
from orbit.agent.application.review_trace import REVIEW_SPAN
from orbit.agent.application.task_trace import TASK_SPAN
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.merge_pass import COUNTED_OUTCOMES
from orbit.agent.domain.reviews import (
    BatchSnapshot,
    Finding,
    LaneSnapshot,
    PlannerSnapshot,
    ReviewGroup,
    ReviewJob,
    ReviewLimits,
    ReviewStage,
)
from orbit.agent.domain.roles import Role, RoleTools
from orbit.agent.domain.tools import (
    COMPLETE_TASK,
    DISMISS_FINDING,
    GET_TASK,
    GIVE_UP,
    REPORT_FINDING,
    REPORT_PROGRESS,
    SUGGEST_TASK,
)
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    REVIEWER,
    ActivateSession,
    AddTask,
    CancelTurn,
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
    LINT,
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
    board_of,
    claims_done,
    gives_up,
    keeps_claiming,
    send,
    session_for,
    settle,
    start,
    with_orbit_tools,
)
from tests.loop_quiet import quiet_within
from tests.merge_hold_helpers import STRESS, LandingTree, SetSteps, parts_of, status, waits_for
from tests.session_builders import last_of, opened, saved, store_with
from tests.telemetry_helpers import Traced
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
        tools=RoleTools(orbit=LENS_TOOLS),
        blocking_findings=blocking,
    )


LENS_ROLES = {**ROLES, BUGS: lens(blocking=True), TESTS: lens(blocking=False)}
"""The built-in roles, with a bugs lens, which may report blocking findings, and a tests lens,
which may not."""


MERGE_TOOLS = (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP, SUGGEST_TASK, DISMISS_FINDING)

MERGING_ROLES = {
    **LENS_ROLES,
    REVIEWER: with_orbit_tools(LENS_ROLES[REVIEWER], MERGE_TOOLS),
}
"""The lens roles, with a reviewer that may dismiss findings, as a merge pass does."""


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


OFF = ("review", "merge-pass")
"""The triggers :func:`lens_triggers` disables: the single reviewer's and the shipped merge
pass, as these tests' lenses and merge pass are their own."""


def lens_triggers(**more: Trigger) -> dict[str, Trigger]:
    """The triggers of :data:`~tests.agent_fakes.TRIGGERS` with the review and the shipped merge
    pass disabled, a bugs and a tests lens reviewing each commit, and ``more``."""
    off = {name: TRIGGERS[name].model_copy(update={"enabled": False}) for name in OFF}
    return {
        **TRIGGERS,
        **off,
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


Decision = tuple[str, tuple[str, ...]] | tuple[str, tuple[str, ...], str]
"""What a merge pass does: files a task titled as the first, the findings the second names, at
the severity the third gives, if it gives one; or, titled ``dismiss``, dismisses the one
finding the second names."""


def merges(*decisions: Decision, replies: list[Reply] | None = None) -> Script:
    """A merge pass that makes ``decisions``, recording orbit's replies in ``replies``, if
    given, with the reply to its claim that it's done last."""

    async def merge(call: Call) -> TurnEndReason:
        await call("get_task")
        for decision in decisions:
            reply = await _decide(call, decision)
            if replies is not None:
                replies.append(reply)
        done = await call("complete_task", summary="Merged")
        if replies is not None:
            replies.append(done)
        return "completed"

    return merge


async def _decide(call: Call, decision: Decision) -> Reply:
    """Make ``decision`` through ``call``."""
    title, findings, *severity = decision
    if title == "dismiss":
        return await call("dismiss_finding", finding=findings[0], reason="Not worth a task.")
    options = {"severity": severity[0]} if severity else {}
    prompt = f"Please {title.lower()}."
    return await call(
        "suggest_task", title=title, prompt=prompt, findings=list(findings), **options
    )


def lensing(*scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any) -> WorkspaceHost:
    """A session landing work in a tree and reviewing it by lens, with the triggers of
    :func:`lens_triggers` unless ``options`` give others, its tasks played by ``scripts`` and
    its reviews by ``reviewers``, in a :class:`FakeTree` unless ``options`` give a ``tree``;
    the other ``options`` go to :func:`session_for`."""
    defaults: dict[str, Any] = {
        "roles": LENS_ROLES,
        "triggers": lens_triggers(),
        "tree": FakeTree(),
    }
    session, _, _ = session_for(*scripts, reviewers=reviewers, review=True, **(defaults | options))
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
        reports(("No test for it", "normal")),
        claims_done,  # The suggestion.
        reports(),  # The batch's review, by both lenses.
        reports(),
        reviewers=(
            merges(
                ("Fix the crash", ("F1",)),
                ("Add a test", ("F2",)),
                ("dismiss", ("F2",)),
                replies=replies,
            ),
        ),
        roles=MERGING_ROLES,
        triggers=lens_triggers(merge=MERGE),
        review_limits=ReviewLimits(suggestions=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies][:3] == [
        "Queued as task 5.",
        "This review has already suggested 1 tasks, the most allowed.",
        "Dismissed F2.",
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


async def test_review_commit_takes_a_landings_own_review_and_review_batch_a_batchs() -> None:
    on_commits = lens_trigger(TESTS).model_copy(update={"when": {"review": "commit"}})
    on_batches = lens_trigger(TESTS).model_copy(update={"when": {"review": "batch"}})
    session = lensing(
        claims_done,
        reports(("Crash on empty input", "blocking")),
        reports(),
        claims_done,  # The suggestion.
        reports(),  # The batch's review, by the bugs lens and the batch's tests lens.
        reports(),
        reviewers=(merges(("Fix the crash", ("F1",))),),
        roles=MERGING_ROLES,
        triggers=lens_triggers(merge=MERGE, tests=on_commits, fixes=on_batches),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, "completed"),
        (BUGS, "bugs", None, "completed"),
        (TESTS, "tests", None, "completed"),
        (REVIEWER, "merge", None, "completed"),
        (CODER, "merge", "batch-1", "completed"),
        (BUGS, "bugs", None, "completed"),
        (TESTS, "fixes", None, "completed"),
    ]


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


class HeldLenses:
    """Agents that claim their tasks done at once, but for lenses, each of which, once it has its
    task, reports a finding titled by its commit and role, such as ``c1 bugs-reviewer``, then
    waits for :meth:`gate` of that title before claiming done; :attr:`running` holds the titles
    of the lenses waiting."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.running: set[str] = set()

    def gate(self, look: str) -> asyncio.Event:
        """The gate the lens titled ``look`` waits for."""
        return self.gates.setdefault(look, asyncio.Event())

    async def play(self, call: Call) -> TurnEndReason:
        """Play a task: a lens's as the class says, any other's by claiming it done."""
        task = await call("get_task")
        found = LOOK.search(task.text)
        if found is not None:
            await self._look(call, f"{found[1]} {found[2]}")
        await call("complete_task", summary="Done")
        return "completed"

    async def _look(self, call: Call, look: str) -> None:
        """Report a finding titled ``look``, then wait for its gate."""
        await call("report_finding", title=look, body=f"Details of {look}.", severity="normal")
        self.running.add(look)
        await self.gate(look).wait()
        self.running.discard(look)


LOOK = re.compile(r"Look at (c\d+) as ([\w-]+)\.")
"""A lens's prompt, as :func:`lens_trigger` words it, with the commit and the role."""


def merges_beside(lenses: HeldLenses, seen: list[tuple[str, frozenset[str]]]) -> Script:
    """A merge pass recording in ``seen`` its task and the ``lenses`` running as it got it, then
    claiming done."""

    async def merge(call: Call) -> TurnEndReason:
        task = await call("get_task")
        seen.append(
            (task.text.split("\n\nWhen you have finished", 1)[0], frozenset(lenses.running))
        )
        await call("complete_task", summary="Merged")
        return "completed"

    return merge


def merge_prompt(commit: str) -> str:
    """The merge pass's prompt for a review of ``commit`` whose bugs and tests lenses each
    reported one finding as :class:`HeldLenses` titles it."""
    findings = "\n\n".join(
        f"F{n} · {role} · normal: {commit} {role}\nDetails of {commit} {role}."
        for n, role in enumerate((BUGS, TESTS), start=1)
    )
    return (
        f"Merge the findings of the review of commit {commit}\n\n{findings}\n\nAt most 5, a merge."
    )


def open_batches(events: list[Event]) -> list[int]:
    """The open batches each review status in ``events`` counted, in order."""
    return [event.open_batches for event in events if isinstance(event, ReviewStatus)]


async def test_reviews_of_two_commits_side_by_side_each_fan_in_on_their_own_lenses() -> None:
    lenses = HeldLenses()
    merged: list[tuple[str, frozenset[str]]] = []
    store = FakeStore()
    session = lensing(
        *(lenses.play for _ in range(6)),
        reviewers=(merges_beside(lenses, merged), merges_beside(lenses, merged)),
        triggers=lens_triggers(merge=MERGE),
        store=store,
    )
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    c1_bugs, c1_tests = f"c1 {BUGS}", f"c1 {TESTS}"
    c2_bugs, c2_tests = f"c2 {BUGS}", f"c2 {TESTS}"
    await eventually(lambda: lenses.running == {c1_bugs, c1_tests, c2_bugs})  # Three slots.
    lenses.gate(c1_bugs).set()
    await eventually(lambda: lenses.running == {c1_tests, c2_bugs, c2_tests})
    lenses.gate(c2_bugs).set()
    lenses.gate(c2_tests).set()
    await eventually(lambda: open_batches(events) == [1, 2, 1])  # c2's batch closed.
    assert merged == [(merge_prompt("c2"), frozenset({c1_tests}))]
    lenses.gate(c1_tests).set()
    await asyncio.wait_for(collecting, 10)
    await session.close()

    assert merged[1:] == [(merge_prompt("c1"), frozenset())]
    assert [t.task.prompt for t in board(events) if t.task.role == REVIEWER] == [
        merge_prompt("c2"),
        merge_prompt("c1"),
    ]
    assert open_batches(events) == [1, 2, 1, 0]
    reviews = reviews_of(store)
    c1_review = {"3": "batch-1", "4": "batch-1", "8": "batch-1"}
    c2_review = {"5": "batch-2", "6": "batch-2", "7": "batch-2"}
    assert reviews.batches == c1_review | c2_review
    assert (reviews.groups, reviews.planner.batches) == ({}, {})


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


# Merge passes deciding findings

THREE_FINDINGS = (
    reports(("Crash on empty input", "blocking")),
    reports(("No test for it", "normal"), ("Flaky timing", "normal")),
)
"""A bugs lens reporting F1 and a tests lens reporting F2 and F3."""


def merging(*scripts: Script, merge: Script, **options: Any) -> WorkspaceHost:
    """A session whose task 1 lands and whose lenses report :data:`THREE_FINDINGS`, its merge
    pass played by ``merge`` and its later tasks by ``scripts``; ``options`` go to
    :func:`lensing`."""
    defaults: dict[str, Any] = {"roles": MERGING_ROLES, "triggers": lens_triggers(merge=MERGE)}
    return lensing(
        claims_done, *THREE_FINDINGS, *scripts, reviewers=(merge,), **(defaults | options)
    )


def span_attributes(span: ReadableSpan) -> Mapping[str, Any]:
    """``span``'s attributes."""
    assert span.attributes is not None
    return span.attributes


def outcomes(lane: LaneSnapshot) -> list[tuple[str, str, str | None, str | None]]:
    """Each finding of ``lane``'s group of batch-1: its id, outcome, task and reason."""
    return [(f.id, f.outcome, f.task, f.reason) for f in lane.groups["batch-1"].findings]


def watching(store: FakeStore, seen: list[LaneSnapshot], script: Script = claims_done) -> Script:
    """A task that notes the review state last saved in ``store`` in ``seen`` as it starts,
    then plays ``script``."""

    async def watch(call: Call) -> TurnEndReason:
        seen.append(reviews_of(store))
        return await script(call)

    return watch


async def test_a_merge_pass_files_merges_and_dismisses_every_finding_then_completes() -> None:
    store, replies, seen = FakeStore(), list[Reply](), list[LaneSnapshot]()
    merge = merges(
        ("Fix the crash", ("F1", "F3"), "blocking"), ("dismiss", ("F2",)), replies=replies
    )
    session = merging(watching(store, seen), reports(), reports(), merge=merge, store=store)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(reply.text, reply.is_error) for reply in replies] == [
        ("Queued as task 5.", False),
        ("Dismissed F2.", False),
        ("All verification steps passed. End your turn now.", False),
    ]
    assert outcomes(seen[0]) == [
        ("F1", "filed", "5", None),
        ("F2", "dismissed", None, "Not worth a task."),
        ("F3", "merged", "5", None),
    ]
    assert seen[0].severities == {"5": "blocking"}
    assert shown(board(events))[3:5] == [
        (REVIEWER, "merge", None, "completed"),
        (CODER, "merge", "batch-1", "completed"),
    ]
    assert (reviews_of(store).groups, reviews_of(store).severities) == ({}, {})


async def test_a_merge_pass_completing_with_a_finding_undecided_is_refused_naming_it() -> None:
    store, replies, seen = FakeStore(), list[Reply](), list[LaneSnapshot]()
    merge = merges(("Fix the crash", ("F1", "F3")), replies=replies)
    session = merging(watching(store, seen), reports(), reports(), merge=merge, store=store)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert (replies[-1].text, replies[-1].is_error) == (
        "Decide every finding first: F2 has no outcome. File them with suggest_task or dismiss"
        " them with dismiss_finding.",
        True,
    )
    # The agent ended its turn regardless: the finding it left is unanswered.
    assert outcomes(seen[0])[1] == ("F2", "unanswered", None, None)


async def test_completing_with_findings_undecided_names_them_all() -> None:
    replies: list[Reply] = []
    session = merging(merge=merges(replies=replies))
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert replies[-1].text == (
        "Decide every finding first: F1, F2, F3 have no outcome. File them with suggest_task or"
        " dismiss them with dismiss_finding."
    )


async def test_suggestions_naming_no_unknown_or_decided_findings_are_refused() -> None:
    replies: list[Reply] = []
    merge = merges(
        ("Fix nothing", ()),
        ("Fix a ghost", ("F9",)),
        ("dismiss", ("F1",)),
        ("Fix the crash", ("F1",)),
        ("Fix the tests", ("F2", "F1")),
        ("dismiss", ("F1",)),
        ("dismiss", ("F9",)),
        ("dismiss", ("F2",)),
        ("dismiss", ("F3",)),
        replies=replies,
    )
    session = merging(merge=merge)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(reply.text, reply.is_error) for reply in replies] == [
        (
            "Name the findings the task covers: at least one of this review's undecided findings.",
            True,
        ),
        ("F9 isn't an undecided finding of this review.", True),
        ("Dismissed F1.", False),
        ("F1 is already dismissed.", True),
        ("F1 is already dismissed.", True),
        ("F1 is already dismissed.", True),
        ("F9 isn't an undecided finding of this review.", True),
        # F2 was named in a refused suggestion, which decided nothing.
        ("Dismissed F2.", False),
        ("Dismissed F3.", False),
        ("All verification steps passed. End your turn now.", False),
    ]
    assert len(board(events)) == 4  # Nothing was filed.


async def test_a_suggestion_refused_as_a_repeat_leaves_its_findings_undecided() -> None:
    replies: list[Reply] = []
    merge = merges(
        ("Make it faster.", ("F1",)),
        ("dismiss", ("F1",)),
        ("dismiss", ("F2",)),
        ("dismiss", ("F3",)),
        replies=replies,
    )
    session = merging(merge=merge)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies[:2]] == ["Task 1 already covers that.", "Dismissed F1."]


async def test_a_merge_pass_that_gives_up_leaves_its_undecided_findings_unanswered() -> None:
    store, seen = FakeStore(), list[LaneSnapshot]()

    async def files_then_gives_up(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Fix the crash", prompt="", findings=["F1"])
        await call("give_up", reason="Too much to merge.")
        return "completed"

    session = merging(
        watching(store, seen), reports(), reports(), merge=files_then_gives_up, store=store
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert board(events)[3].status == "gave_up"
    assert outcomes(seen[0]) == [
        ("F1", "filed", "5", None),
        ("F2", "unanswered", None, None),
        ("F3", "unanswered", None, None),
    ]


async def test_a_merge_pass_cancelled_leaves_its_undecided_findings_unanswered() -> None:
    store, seen, gate = FakeStore(), list[LaneSnapshot](), asyncio.Event()
    filed = asyncio.Event()

    async def files_then_waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Fix the crash", prompt="", findings=["F1"])
        filed.set()
        await gate.wait()
        return "completed"

    session = merging(
        watching(store, seen), reports(), reports(), merge=files_then_waits, store=store
    )
    await start(session)
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await asyncio.wait_for(filed.wait(), 10)
    await send(session, CancelTurn(lane="review"))
    await asyncio.wait_for(collecting, 10)
    await session.close()

    assert board(events)[3].status == "cancelled"
    assert [outcome for _, outcome, _, _ in outcomes(seen[0])] == [
        "filed",
        "unanswered",
        "unanswered",
    ]


async def test_a_filed_task_landing_fixes_its_findings_and_one_giving_up_gives_them_up() -> None:
    store, seen = FakeStore(), list[LaneSnapshot]()
    merge = merges(
        ("Fix the crash", ("F1", "F3")),
        ("Add a test", ("F2",)),
        ("Note the timing", ("F9",)),
    )
    session = merging(
        claims_done,
        watching(store, seen, gives_up),
        reports(),
        reports(),
        merge=merge,
        store=store,
    )
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert outcomes(seen[0]) == [
        ("F1", "fixed", "5", None),
        ("F2", "filed", "6", None),
        ("F3", "fixed", "5", None),
    ]
    assert reviews_of(store).groups == {}


async def test_spans_count_lens_findings_merge_outcomes_and_the_findings_fixed() -> None:
    traced = Traced()
    merge = merges(("Fix the crash", ("F1", "F3")), ("dismiss", ("F2",)))
    session = merging(claims_done, reports(), reports(), merge=merge, telemetry=traced.telemetry)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    by_role = [span_attributes(span) for span in traced.spans(REVIEW_SPAN)]
    lenses = [
        (a["orbit.role"], a.get("orbit.lens"), a.get("orbit.review.findings")) for a in by_role
    ]
    assert lenses == [
        (BUGS, BUGS, 1),
        (TESTS, TESTS, 2),
        (REVIEWER, None, None),
        (BUGS, BUGS, 0),
        (TESTS, TESTS, 0),
    ]
    merged = by_role[2]
    assert [merged[f"orbit.findings.{name}"] for name in COUNTED_OUTCOMES] == [1, 1, 1, 0]
    assert "orbit.findings.filed" not in by_role[0]
    tasks = {
        span_attributes(s)["orbit.task.id"]: span_attributes(s) for s in traced.spans(TASK_SPAN)
    }
    assert tasks["5"]["orbit.findings.fixed"] == 2
    assert "orbit.findings.fixed" not in tasks["1"]


async def test_a_filed_task_giving_up_counts_its_findings_given_up_on_its_span() -> None:
    traced = Traced()
    merge = merges(("Fix the crash", ("F1", "F2", "F3")))
    session = merging(gives_up, merge=merge, telemetry=traced.telemetry)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    tasks = {
        span_attributes(s)["orbit.task.id"]: span_attributes(s) for s in traced.spans(TASK_SPAN)
    }
    assert tasks["5"]["orbit.findings.given_up"] == 3


def fixing(
    fix: Script, store: FakeStore, seen: list[LaneSnapshot], traced: Traced
) -> WorkspaceHost:
    """A session whose merge pass files task 5 for F1 and F3, and task 6 for F2, whose task 5
    fails verification after its one fix, falling back to fix task 7, played by ``fix``, and
    whose task 6 notes the review state saved in ``store`` in ``seen`` as it starts; traced by
    ``traced``."""
    steps = SetSteps()
    steps.task_results = [True, False, False]  # Task 1 passes, task 5 fails twice, then all pass.
    return merging(
        keeps_claiming,
        fix,
        watching(store, seen),
        reports(),
        reports(),
        merge=merges(("Fix the crash", ("F1", "F3")), ("Add a test", ("F2",))),
        verification=Verification((LINT,), steps),
        limits=Limits(max_attempts=1),
        store=store,
        telemetry=traced.telemetry,
    )


async def test_a_fix_of_a_filed_task_landing_fixes_the_findings_filed_in_it() -> None:
    traced, store, seen = Traced(), FakeStore(), list[LaneSnapshot]()
    session = fixing(claims_done, store, seen, traced)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    fix = board_of(session).get("7")
    assert (fix.fixes, fix.status) == ("5", "completed")
    assert outcomes(seen[0]) == [
        ("F1", "fixed", "5", None),
        ("F2", "filed", "6", None),
        ("F3", "fixed", "5", None),
    ]
    tasks = {
        span_attributes(s)["orbit.task.id"]: span_attributes(s) for s in traced.spans(TASK_SPAN)
    }
    assert tasks["7"]["orbit.findings.fixed"] == 2
    assert "orbit.findings.fixed" not in tasks["5"]


async def test_a_fix_of_a_filed_task_giving_up_gives_up_the_findings_filed_in_it() -> None:
    traced, store, seen = Traced(), FakeStore(), list[LaneSnapshot]()
    session = fixing(gives_up, store, seen, traced)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    fix = board_of(session).get("7")
    assert (fix.fixes, fix.status) == ("5", "gave_up")
    assert outcomes(seen[0]) == [
        ("F1", "given_up", "5", None),
        ("F2", "filed", "6", None),
        ("F3", "given_up", "5", None),
    ]
    tasks = {
        span_attributes(s)["orbit.task.id"]: span_attributes(s) for s in traced.spans(TASK_SPAN)
    }
    assert tasks["7"]["orbit.findings.given_up"] == 2


async def test_a_filed_task_landing_stacked_after_a_hold_counts_nothing_on_the_later_task() -> None:
    traced, store, seen = Traced(), FakeStore(), list[LaneSnapshot]()
    filed_done = asyncio.Event()
    merge = merges(("Fix the crash", ("F1", "F3")), ("Add a test", ("F2",)))
    later = [watching(store, seen) for _ in range(6)]
    session = merging(
        waits_for(filed_done),
        claims_done,
        *later,
        merge=merge,
        verification=Verification((LINT,), SetSteps(stress=[False, True]), sets=(STRESS,)),
        tree=LandingTree(),
        store=store,
        telemetry=traced.telemetry,
    )
    sets, holds = parts_of(session)
    await start(session)
    await eventually(
        lambda: len(board_of(session).tasks) > 4 and status(session, "5")() == "running"
    )
    sets.run("stress", "nightly")
    await eventually(lambda: holds.held and len(board_of(session).tasks) == 7)
    filed_done.set()
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert outcomes(seen[0]) == [
        ("F1", "fixed", "5", None),
        ("F2", "filed", "6", None),
        ("F3", "fixed", "5", None),
    ]
    tasks = {
        span_attributes(s)["orbit.task.id"]: span_attributes(s) for s in traced.spans(TASK_SPAN)
    }
    fix = board_of(session).get("7")
    assert (fix.origin, fix.status) == ("nightly", "completed")
    assert "orbit.findings.fixed" not in tasks["7"]
    assert "orbit.findings.fixed" not in tasks["5"]


async def test_a_merge_pass_ending_unanswered_counts_them_on_its_span() -> None:
    traced = Traced()
    session = merging(merge=gives_up, telemetry=traced.telemetry)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    [merged] = [
        span_attributes(s)
        for s in traced.spans(REVIEW_SPAN)
        if span_attributes(s)["orbit.role"] == REVIEWER
    ]
    assert [merged[f"orbit.findings.{name}"] for name in COUNTED_OUTCOMES] == [0, 0, 0, 3]


async def test_a_reviewer_that_isnt_a_merge_pass_files_without_findings_and_cant_dismiss() -> None:
    replies: list[Reply] = []
    merge = merges(
        ("Fix the crash", ()), ("Fix it", ("F1",)), ("dismiss", ("F1",)), replies=replies
    )
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(merge,),
        roles=MERGING_ROLES,
        tree=FakeTree(),
        review=True,
    )
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [(reply.text, reply.is_error) for reply in replies] == [
        ("Queued as task 3.", False),
        ("Only a merge pass files findings; suggest the task without them.", True),
        ("Only a merge pass can dismiss findings.", True),
        ("All verification steps passed. End your turn now.", False),
    ]


async def test_a_dismissal_without_a_reason_is_refused() -> None:
    replies: list[Reply] = []

    async def dismisses_without_reason(call: Call) -> TurnEndReason:
        await call("get_task")
        replies.append(await call("dismiss_finding", finding="F1", reason=" "))
        return "completed"

    session = merging(merge=dismisses_without_reason)
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert (replies[0].text, replies[0].is_error) == ("A dismissal needs a reason.", True)


async def test_a_lens_running_in_the_work_lane_reports_into_its_review_untraced() -> None:
    traced, replies = Traced(), list[Reply]()
    working = LENS_ROLES[TESTS].model_copy(update={"lane": "work", "workspace": "worktree"})
    session = lensing(
        claims_done,
        reports(),
        reports(("No test for it", "normal"), replies=replies),
        roles={**LENS_ROLES, TESTS: working},
        telemetry=traced.telemetry,
    )
    await start(session)
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert [reply.text for reply in replies] == ["Recorded as F1."]
    assert [span_attributes(s)["orbit.role"] for s in traced.spans(REVIEW_SPAN)] == [BUGS]


async def test_a_dismissal_is_refused_while_reviews_are_off() -> None:
    replies: list[Reply] = []
    merge = merges(("dismiss", ("F1",)), replies=replies)
    session, _, _ = session_for(reviewers=(merge,), roles=MERGING_ROLES)
    await send(session, AddTask(task=TaskSpec(prompt="Merge.", role=REVIEWER)))
    await send(session, RunTasks())
    await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert replies[0].text == "Orbit isn't taking dismissals."


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
    await quiet_within(0.05)
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


def lenses_ended(stage: ReviewStage) -> tuple[tuple[TaskState, ...], LaneSnapshot]:
    """A board and review lane saved as the review of task 1's commit had every lens ended, its
    bugs lens having found :data:`FOUND`, and its group at ``stage``, with no merge pass
    booked."""
    tasks, lane = mid_review("completed")
    group = replace(lane.groups["batch-1"], ended=("2", "3"), stage=stage)
    return tasks, lane.model_copy(update={"groups": {"batch-1": group}})


async def test_a_review_saved_merging_with_findings_and_no_merge_pass_books_one_on_load() -> None:
    tasks, lane = lenses_ended("merging")
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, launcher = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await quiet_within(0.05)
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert shown(snapshot.tasks)[3:] == [(REVIEWER, "merge", None, "pending")]
    assert snapshot.review == ReviewStatus(paused=False, open_batches=1)
    assert reviews_of(store).groups["batch-1"].merges == ("4",)
    assert reviews_of(store).batches["4"] == "batch-1"
    assert launcher.launches == []


async def test_a_review_saved_merging_with_findings_and_no_merge_trigger_closes_on_load() -> None:
    tasks, lane = lenses_ended("merging")
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, launcher = reopening(store, triggers=lens_triggers())
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert len(snapshot.tasks) == 3
    assert snapshot.review == ReviewStatus(paused=False, open_batches=0)
    assert launcher.launches == []


async def test_a_review_saved_merging_with_no_findings_closes_on_load() -> None:
    tasks, lane = lenses_ended("merging")
    group = replace(lane.groups["batch-1"], findings=())
    quiet = lane.model_copy(update={"groups": {"batch-1": group}})
    store = store_with(saved("auth", *tasks, reviews=quiet))
    session, launcher = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert len(snapshot.tasks) == 3
    assert snapshot.review == ReviewStatus(paused=False, open_batches=0)
    assert launcher.launches == []


async def test_a_review_saved_with_every_lens_ended_fans_in_once_on_load() -> None:
    tasks, lane = lenses_ended("reviewing")
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, launcher = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await quiet_within(0.05)
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert shown(snapshot.tasks)[3:] == [(REVIEWER, "merge", None, "pending")]
    assert snapshot.review == ReviewStatus(paused=False, open_batches=1)
    assert reviews_of(store).groups["batch-1"].stage == "merging"
    assert reviews_of(store).batches["4"] == "batch-1"
    assert launcher.launches == []


async def test_a_review_saved_with_a_lens_pending_stays_open_on_load() -> None:
    tasks, lane = mid_review("pending")
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, _ = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert len(snapshot.tasks) == 3
    assert snapshot.review == ReviewStatus(paused=False, open_batches=1)
    assert reviews_of(store).groups["batch-1"].stage == "reviewing"


@pytest.mark.parametrize("status", ["completed", "unverified", "gave_up", "cancelled", "error"])
async def test_a_review_saved_with_its_last_lens_finished_on_the_board_fans_in_on_load(
    status: TaskStatus,
) -> None:
    tasks, lane = mid_review(status)
    store = store_with(saved("auth", *tasks, reviews=lane))
    session, launcher = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await quiet_within(0.05)
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert shown(snapshot.tasks)[3:] == [(REVIEWER, "merge", None, "pending")]
    assert snapshot.review == ReviewStatus(paused=False, open_batches=1)
    assert reviews_of(store).groups["batch-1"].ended == ("2", "3")
    assert reviews_of(store).groups["batch-1"].merges == ("4",)
    assert launcher.launches == []


async def test_a_review_saved_with_its_lenses_finished_but_no_findings_closes_on_load() -> None:
    tasks, lane = mid_review("completed")
    group = replace(lane.groups["batch-1"], findings=())
    quiet = lane.model_copy(update={"groups": {"batch-1": group}})
    store = store_with(saved("auth", *tasks, reviews=quiet))
    session, launcher = reopening(store, triggers=lens_triggers())
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    snapshot = last_of(Snapshot, events)
    assert len(snapshot.tasks) == 3
    assert snapshot.review == ReviewStatus(paused=False, open_batches=0)
    assert launcher.launches == []


async def test_a_review_saved_after_its_batch_stopped_filling_is_left_as_saved_on_load() -> None:
    tasks, lane = mid_review("completed")
    closing = PlannerSnapshot(
        next_batch=2, batches={"batch-1": BatchSnapshot(round=1, cap=5, filling=False)}
    )
    store = store_with(saved("auth", *tasks, reviews=lane.model_copy(update={"planner": closing})))
    session, launcher = reopening(store, triggers=lens_triggers(merge=MERGE))
    events = await opened(session, ActivateSession(session="auth"))
    await session.close()

    assert len(last_of(Snapshot, events).tasks) == 3
    assert reviews_of(store).groups["batch-1"] == lane.groups["batch-1"]
    assert launcher.launches == []
