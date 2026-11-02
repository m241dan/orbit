"""Reviews: queued as work lands, run beside the work in snapshots, suggestions run after."""

import asyncio
import itertools
from collections.abc import Sequence
from typing import Any

import pytest

from orbit.agent import WorkspaceHost
from orbit.agent.application.ports import Merge, Reply, TreeFailed
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.work_turns import WorkTurns
from orbit.agent.domain.activity import reported
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.config import load_config
from orbit.agent.domain.endings import BUG, CANCELLED, REVIEW_STOPPED_BY_YOU, STOPPED_BY_YOU
from orbit.agent.domain.reviews import ReviewLimits, SuperReviewCount
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    GIT_FAILED,
    INTERNAL,
    INVALID,
    NOT_CONFIGURED,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    UNKNOWN,
    Activity,
    AddTask,
    AgentExited,
    CancelAgent,
    CancelTurn,
    CommandRefused,
    EditTask,
    ErrorOccurred,
    Event,
    FireTrigger,
    Notice,
    RemoveTask,
    ReviewStatus,
    RunTasks,
    TasksChanged,
    TaskSpec,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    ONE_AT_A_TIME,
    ROLES,
    TRIGGERS,
    Call,
    FakeTree,
    Script,
    ScriptedLauncher,
    Steps,
    Stop,
    board,
    board_of,
    claims_done,
    events_until,
    files,
    gives_up,
    keeps_claiming,
    lane_of,
    of_type,
    queued,
    refused,
    reports,
    send,
    session_for,
    settle,
    start,
    suggests,
    super_every,
    verified,
)
from tests.loop_quiet import quiet_within
from tests.waiting import eventually


def _errored(events: list[Event]) -> bool:
    return bool(_error_messages(events))


def _refused(events: list[Event]) -> bool:
    return bool(_refusals(events))


def _refusals(events: list[Event]) -> list[CommandRefused]:
    return [e for e in events if isinstance(e, CommandRefused)]


def _error_messages(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, ErrorOccurred)]


def _roles_seen(events: list[Event]) -> set[str]:
    """Every role a task on the board had, over all the board's changes."""
    return {t.task.role for e in events if isinstance(e, TasksChanged) for t in e.tasks}


def reviewers_of(launcher: ScriptedLauncher) -> list[str]:
    return [la.agent.id for la in launcher.launches if la.agent.role == "reviewer"]


def statuses_of(events: list[Event], role: str) -> list[str]:
    return [t.status for t in board(events) if t.task.role == role]


def task_is(task_id: str, status: TaskStatus) -> Stop:
    """Stops when task ``task_id`` is published with ``status``."""
    return Stop(
        f"task_is({task_id!r}, {status!r})",
        lambda e: (
            isinstance(e, TasksChanged)
            and any(t.id == task_id and t.status == status for t in e.tasks)
        ),
    )


def latest_board(events: list[Event]) -> tuple[TaskState, ...]:
    """The tasks last published in ``events``, or none if none were."""
    return board(events) if any(isinstance(e, TasksChanged) for e in events) else ()


async def test_landed_work_is_reviewed_and_suggestions_run_and_are_reviewed_in_turn() -> None:
    tree = FakeTree()
    session, _, launcher = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests", "Rename x"), suggests()),
        tree=tree,
        review=True,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)

    shown = [(t.task.role, t.status, t.origin, t.batch, t.round) for t in board(events)]
    assert shown == [
        ("coder", "completed", "manual", None, 0),
        ("reviewer", "completed", "review", None, 0),
        ("coder", "completed", "review", "batch-1", 1),
        ("coder", "completed", "review", "batch-1", 1),
        ("reviewer", "completed", "review", None, 0),
    ]
    _, first_review, suggestion, _, batch_review = board(events)
    assert first_review.task.prompt.startswith("Review commit c1")
    assert "diff of c1" in first_review.task.prompt
    assert suggestion.task.prompt == "Add tests\n\nPlease add tests."
    assert suggestion.task.settings.tool_access == "full"  # The coder role's default.
    assert batch_review.task.prompt.startswith("Review the 2 commits made for review batch-1")
    assert tree.snapshots == ["c1", "c3"]
    assert tree.dropped == ["/work/review-c1", "/work/review-c3"]
    review_launches = [la for la in launcher.launches if la.agent.role == "reviewer"]
    assert [la.cwd for la in review_launches] == ["/work/review-c1", "/work/review-c3"]


async def test_a_review_is_queued_with_the_effort_the_reviewer_role_sets() -> None:
    roles = load_config({"roles": {"reviewer": {"effort": "high"}}}, ROLES).roles
    session, _, _ = session_for(
        claims_done, reviewers=(suggests(),), tree=FakeTree(), review=True, roles=roles
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    [review] = [t for t in board(events) if t.task.role == "reviewer"]
    assert review.task.settings.effort == "high"
    [coder] = [t for t in board(events) if t.task.role == "coder"]
    assert coder.task.settings.effort is None


async def test_review_pauses_after_the_last_round_and_says_so() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["coder", "reviewer", "coder"]
    notices = [e.message for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and "paused" in notices[0]


async def test_suggestions_over_the_cap_or_already_queued_are_refused() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests", "add TESTS", "Rename x", "Tidy", replies=replies),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1, suggestions=2),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [False, True, False, True]
    assert "already" in replies[1].text and "2 tasks" in replies[3].text
    assert len([t for t in board(events) if t.batch]) == 2


async def test_a_review_is_told_which_tasks_are_queued_already() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests(), suggests(), suggests()),
        tree=FakeTree(),
        review=True,
    )
    await start(session, "Make it faster.", "Add tests\n\nFor parse.", "Rename x")
    events = await asyncio.wait_for(settle(session), 10)
    reviews = [t.task.prompt for t in board(events) if t.task.role == "reviewer"]
    assert "Tasks already queued (don't suggest these):\n- Add tests\n- Rename x\n\n" in reviews[0]
    assert "- Add tests" not in reviews[2] and "- Rename x" not in reviews[2]


async def test_a_suggestion_an_earlier_review_made_is_refused_after_its_task_is_removed() -> None:
    replies: list[Reply] = []
    sessions: list[WorkspaceHost] = []

    async def suggests_then_it_is_removed(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please.")
        await send(sessions[0], RemoveTask(task_id="4"))
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests_then_it_is_removed, suggests("add TESTS", "Rename x", replies=replies)),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [True, False]
    assert replies[0].text == "An earlier review already suggested that."
    assert [t.task.prompt.split("\n")[0] for t in board(events) if t.batch] == ["Rename x"]


async def test_a_suggestion_without_a_title_is_refused_and_not_counted() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests(" ", "Add tests", "Rename x", replies=replies), suggests()),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1, suggestions=2),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [True, False, False]
    assert replies[0].text == "A task needs a title."
    assert [t.task.prompt.split("\n")[0] for t in board(events) if t.batch] == [
        "Add tests",
        "Rename x",
    ]


async def test_without_review_nothing_is_reviewed() -> None:
    session, _, _ = session_for(claims_done, tree=FakeTree())
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["coder"]


async def test_a_crashing_reviewer_still_has_its_snapshot_dropped() -> None:
    async def crashes(call: Call) -> TurnEndReason:
        raise RuntimeError("reviewer fell over")

    tree = FakeTree()
    session, _, _ = session_for(claims_done, reviewers=(crashes,), tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.status for t in board(events)] == ["completed", "error"]
    assert tree.dropped == ["/work/review-c1"]


async def test_a_snapshot_that_cannot_be_dropped_leaves_the_review_completed() -> None:
    tree = FakeTree()
    tree.failures["drop"] = TreeFailed("git worktree remove failed")
    session, _, _ = session_for(claims_done, reviewers=(suggests(),), tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.status for t in board(events)] == ["completed", "completed"]
    message = (
        "The snapshot at /work/review-c1 couldn't be removed and is left over:"
        " git worktree remove failed"
    )
    assert ErrorOccurred(code=GIT_FAILED, message=message) in events


async def test_after_cancelling_suggestions_wait_to_be_run() -> None:
    gate = asyncio.Event()

    async def slow_review(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("suggest_task", title="Add tests", prompt="Please.")
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, launcher = session_for(
        claims_done,
        claims_done,
        reviewers=(slow_review,),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1),
    )
    await start(session)
    await eventually(lambda: any(la.agent.role == "reviewer" for la in launcher.launches))
    await send(session, CancelTurn())
    gate.set()

    def reviewed(events: list[Event]) -> bool:
        boards = [e.tasks for e in events if isinstance(e, TasksChanged)]
        return bool(boards) and [t.status for t in boards[-1]] == [
            "completed",
            "completed",
            "pending",
        ]

    events = await asyncio.wait_for(settle(session, until=reviewed), 10)
    assert [t.status for t in board(events)] == ["completed", "completed", "pending"]

    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    assert [t.status for t in board(events)][-1] == "completed"


async def test_closing_stops_a_running_review() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    tree = FakeTree()
    session, _, _ = session_for(claims_done, reviewers=(endless,), tree=tree, review=True)
    await start(session)
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await asyncio.wait_for(reviewing.wait(), 10)
    await asyncio.wait_for(session.close(), 5)
    collecting.cancel()
    events += queued(session)
    assert AgentExited(agent_id="agent-2", reason="killed") in events
    assert tree.dropped == ["/work/review-c1"]


async def test_a_review_the_user_queued_works_in_place_and_cannot_suggest() -> None:
    replies: list[Reply] = []
    session, _, _ = session_for(
        reviewers=(suggests("Add tests", replies=replies),), tree=FakeTree(), review=True
    )
    await send(session, AddTask(task=TaskSpec(prompt="Review the parser", role="reviewer")))
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session), 10)
    assert replies[0].is_error and "suggest" in replies[0].text
    assert [t.status for t in board(events)] == ["completed"]


def test_launcher_scripts_for_reviews_are_kept_apart() -> None:
    launcher = ScriptedLauncher(object(), claims_done, reviewers=(claims_done,))  # type: ignore[arg-type]
    assert (len(launcher.scripts), len(launcher.reviewers)) == (1, 1)


async def test_tasks_of_a_batch_are_fixed_within_it() -> None:
    session, _, _ = session_for(
        claims_done,
        keeps_claiming,
        claims_done,
        verification=verified(Steps(True, False, False, True, True)),
        reviewers=(suggests("Add tests"), suggests()),
        tree=FakeTree(),
        review=True,
        limits=Limits(max_attempts=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    shown = [(t.task.role, t.status, t.batch) for t in board(events)]
    assert shown == [
        ("coder", "completed", None),
        ("reviewer", "completed", None),
        ("coder", "completed", "batch-1"),
        ("coder", "completed", "batch-1"),
        ("reviewer", "completed", None),
    ]


async def test_work_that_committed_nothing_is_not_reviewed() -> None:
    session, _, _ = session_for(claims_done, tree=FakeTree(empty=True), review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == ["coder"]


async def test_a_fix_of_the_users_task_joins_no_batch() -> None:
    session, _, _ = session_for(
        keeps_claiming,
        claims_done,
        verification=verified(Steps(False, False, True)),
        reviewers=(suggests(),),
        tree=FakeTree(),
        review=True,
        limits=Limits(max_attempts=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [(t.task.role, t.batch) for t in board(events)] == [
        ("coder", None),
        ("coder", None),
        ("reviewer", None),
    ]


async def test_closing_before_any_review_ran_is_harmless() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    await session.close()


async def test_reviews_queued_while_one_runs_wait_their_turn() -> None:
    gate = asyncio.Event()

    async def slow(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Reviewed")
        return "completed"

    async def opens_gate(call: Call) -> TurnEndReason:
        gate.set()
        return await claims_done(call)

    session, _, launcher = session_for(
        claims_done, opens_gate, reviewers=(slow, suggests()), tree=FakeTree(), review=True
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    roles = [t.task.role for t in board(events)]
    assert roles.count("reviewer") == 2
    assert [la.agent.role for la in launcher.launches].count("reviewer") == 2


async def _landed_two_while_the_first_review_waited(
    session: WorkspaceHost, gate: asyncio.Event
) -> list[Event]:
    """Run tasks "one" and "two", the second landing while the first's review waits on
    ``gate``, then let the reviews finish; returns the events."""
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: [t.task.role for t in latest_board(events)].count("reviewer") >= 2)
    gate.set()
    await asyncio.wait_for(collecting, 10)
    return events


async def test_a_review_queued_while_another_waits_is_not_told_of_that_review() -> None:
    gate = asyncio.Event()

    async def waits_then_reviews(call: Call) -> TurnEndReason:
        await gate.wait()
        return await suggests()(call)

    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(waits_then_reviews, suggests()),
        tree=FakeTree(),
        review=True,
    )
    events = await _landed_two_while_the_first_review_waited(session, gate)
    reviews = [t.task.prompt for t in board(events) if t.task.role == "reviewer"]
    assert reviews[0].startswith("Review commit c1")
    assert "Review commit" not in reviews[1].split("\n", 1)[1]


def super_reviewing_every_two(gate: asyncio.Event, tree: FakeTree) -> WorkspaceHost:
    """A session in ``tree`` whose super review is due every two of the user's commits, whose
    first commit's review waits for ``gate``, and whose super review's Style lens raises a
    finding its merge pass files, that suggestion's batch then reviewed by Style."""

    async def waits_then_reviews(call: Call) -> TurnEndReason:
        await gate.wait()
        return await suggests()(call)

    session, _, _ = session_for(
        *([claims_done] * 3),
        reports(("The parser is too big", "normal")),
        claims_done,
        claims_done,
        claims_done,  # The suggestion,
        claims_done,  # whose batch the style lens, which raised its finding, reviews.
        reviewers=(
            waits_then_reviews,
            suggests(),
            suggests(),
            files("Split the parser", ["F1"], []),
            suggests(),
        ),
        tree=tree,
        review=True,
        triggers=super_every(2),
    )
    return session


def lenses_of(tasks: Sequence[TaskState]) -> list[tuple[str, str, str | None]]:
    """Each of the super review's tasks among ``tasks``: its role, first line and model."""
    return [
        (t.task.role, t.task.prompt.split("\n", 1)[0], t.task.settings.model)
        for t in tasks
        if t.origin == SUPER_REVIEW
    ]


async def test_every_so_many_of_the_users_commits_the_whole_codebase_is_super_reviewed() -> None:
    gate = asyncio.Event()
    tree = FakeTree()
    session = super_reviewing_every_two(gate, tree)
    events = await _landed_two_while_the_first_review_waited(session, gate)
    seen = _roles_seen(events)
    assert not seen & set(SUPER_LENS_REVIEWERS)  # Due at c2, but a review was pending.

    await send(session, AddTask(task=TaskSpec(prompt="three")))
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    tasks = board(events)
    # From its trigger, on the super lenses' default model.
    assert lenses_of(tasks) == [
        (role, "Super review of the codebase at c3", "opus") for role in SUPER_LENS_REVIEWERS
    ]
    [suggestion] = [t for t in tasks if t.batch]
    assert (suggestion.origin, suggestion.round) == ("merge-pass", 1)
    assert "c3" in tree.snapshots


async def test_a_super_review_can_be_asked_for_before_any_task_ran() -> None:
    # The coder's tree doesn't exist yet, so its head can't be read; the base's commit can.
    tree = FakeTree()
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    assert [t.task.role for t in board(events)] == list(SUPER_LENS_REVIEWERS)
    assert tree.snapshots == [tree.base] * len(SUPER_LENS_REVIEWERS) and tree.prepared == []
    assert not any(isinstance(e, ErrorOccurred) for e in events)


SINCE_SUPER = "Commits since the last super review:"
FILES = "Files they changed:"


LENSES = (claims_done,) * len(SUPER_LENS_REVIEWERS)
"""The scripts of one super review's lenses, each finding nothing."""


def super_task(events: list[Event]) -> TaskState:
    """The last super review lens task published in ``events``, whose prompt the other lenses
    of its review share."""
    return [t for t in board(events) if t.task.role in SUPER_LENS_REVIEWERS][-1]


async def test_the_first_super_review_lists_no_commits_and_the_next_starts_from_its_own() -> None:
    tree = FakeTree()
    tree.since[tree.base] = ["Add B"]
    tree.changed[tree.base] = ["b.py"]
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    review = super_task(events)
    assert review.status == "completed"  # It ran, and wasn't told what landed as it started.
    prompt = review.task.prompt
    assert "Landed since" not in prompt and SINCE_SUPER not in prompt and FILES not in prompt
    assert tree.asked_since == [] and tree.asked_changed == []
    assert lane_of(session).planner.last_super_commit == tree.base


async def test_the_next_super_review_lists_the_commits_since_the_last_and_their_files() -> None:
    tree = FakeTree()
    tree.since[tree.base] = ["Add D", "Add E"]
    tree.changed[tree.base] = ["src/e.py", "README.md", "src/d.py"]
    session, _, _ = session_for(*LENSES * 2, tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    tree.base = "e"
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session, events), 10)
    assert super_task(events).task.prompt.endswith(
        f"{SINCE_SUPER}\n- Add D\n- Add E\n\n{FILES}\n- README.md\n- src/d.py\n- src/e.py"
    )
    assert tree.asked_since == [("base", "e")]  # Up to the base's commit it reviews.
    assert tree.asked_changed == [("base", "e")]
    assert lane_of(session).planner.last_super_commit == "e"


async def test_a_super_review_due_lists_the_commits_since_the_last() -> None:
    tree = FakeTree()
    tree.since["s0"] = ["Add D"]
    tree.changed["s0"] = ["d.py"]
    session, _, _ = session_for(
        claims_done,
        *LENSES,
        reviewers=(suggests(),),
        tree=tree,
        review=True,
        triggers=super_every(1),
    )
    lane_of(session).planner.last_super_commit = "s0"
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert super_task(events).task.prompt.endswith(f"{SINCE_SUPER}\n- Add D\n\n{FILES}\n- d.py")
    # Up to the commit it reviews, not the base's; c1's own review asks up to the base's.
    assert tree.asked_since == [("s0", "c1"), ("c1", None)]
    assert tree.asked_changed == [("s0", "c1")]
    assert lane_of(session).planner.last_super_commit == "c1"


async def test_a_super_review_with_nothing_landed_since_the_last_lists_no_commits() -> None:
    tree = FakeTree()
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    lane_of(session).planner.last_super_commit = "s1"
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    prompt = super_task(events).task.prompt
    assert SINCE_SUPER not in prompt and FILES not in prompt
    assert tree.asked_since == tree.asked_changed == [("s1", tree.base)]
    assert lane_of(session).planner.last_super_commit == tree.base


@pytest.mark.parametrize("method", ["landed_since", "changed_files"])
async def test_a_super_review_whose_commits_since_cannot_be_listed_is_queued_without(
    method: str,
) -> None:
    tree = FakeTree()
    tree.since["s1"] = ["Add D"]
    tree.changed["s1"] = ["d.py"]
    tree.failures[method] = TreeFailed("git failed: bad revision")
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    lane_of(session).planner.last_super_commit = "s1"
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    review = super_task(events)
    assert review.status == "completed"
    assert SINCE_SUPER not in review.task.prompt and FILES not in review.task.prompt
    assert not _errored(events)
    assert lane_of(session).planner.last_super_commit == tree.base


SAMPLED = "Sampled files (read these too):"
TRACKED = ["src/a.py", "src/b.cpp", "docs/x.md", "uv.lock", "tests/t.py"]
SRC = (SkillMapping(paths=("src",), use=("python",)),)


async def super_review_prompt(tree: FakeTree, **options: Any) -> str:
    """The task of the super review the user asks for in a session on ``tree``, whose other
    ``options`` go to :func:`session_for`."""
    session, _, _ = session_for(*LENSES, tree=tree, review=True, **options)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    return super_task(events).task.prompt


async def test_a_super_review_samples_the_code_files_under_the_mapped_paths() -> None:
    tree = FakeTree()
    tree.tracked[tree.base] = TRACKED
    prompt = await super_review_prompt(tree, skill_mappings=SRC)
    assert prompt.endswith(f"at base\n\n{SAMPLED}\n- src/a.py\n- src/b.cpp")


async def test_a_super_review_without_mapped_paths_samples_every_code_file() -> None:
    tree = FakeTree()
    tree.tracked[tree.base] = TRACKED
    prompt = await super_review_prompt(tree)
    assert prompt.endswith(f"{SAMPLED}\n- src/a.py\n- src/b.cpp\n- tests/t.py")


async def test_a_super_review_samples_no_file_changed_since_the_last() -> None:
    tree = FakeTree()
    tree.since["s1"] = ["Add A"]
    tree.changed["s1"] = ["src/a.py"]
    tree.tracked[tree.base] = TRACKED
    session, _, _ = session_for(*LENSES, tree=tree, review=True, skill_mappings=SRC)
    lane_of(session).planner.last_super_commit = "s1"
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    assert super_task(events).task.prompt.endswith(
        f"{SINCE_SUPER}\n- Add A\n\n{FILES}\n- src/a.py\n\n{SAMPLED}\n- src/b.cpp"
    )


async def test_a_super_review_samples_as_many_files_as_its_limit_allows() -> None:
    tree = FakeTree()
    tree.tracked[tree.base] = TRACKED
    prompt = await super_review_prompt(tree, review_limits=ReviewLimits(super_sample=2))
    sampled = prompt.split(f"{SAMPLED}\n", 1)[1].splitlines()
    assert len(set(sampled)) == len(sampled) == 2
    assert set(sampled) <= {"- src/a.py", "- src/b.cpp", "- tests/t.py"}


async def test_a_super_review_with_a_sample_size_of_zero_lists_no_sample() -> None:
    tree = FakeTree()
    tree.tracked[tree.base] = TRACKED
    prompt = await super_review_prompt(tree, review_limits=ReviewLimits(super_sample=0))
    assert SAMPLED not in prompt


async def test_a_super_review_whose_tracked_files_cannot_be_listed_has_no_sample() -> None:
    tree = FakeTree()
    tree.since["s1"] = ["Add D"]
    tree.tracked[tree.base] = TRACKED
    tree.failures["tracked_files"] = TreeFailed("git ls-tree failed: bad revision")
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    lane_of(session).planner.last_super_commit = "s1"
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    review = super_task(events)
    assert review.status == "completed"
    assert review.task.prompt.endswith(f"{SINCE_SUPER}\n- Add D")
    assert not _errored(events)


async def test_a_super_review_whose_commits_since_cannot_be_listed_still_samples() -> None:
    tree = FakeTree()
    tree.since["s1"] = ["Add D"]
    tree.tracked[tree.base] = ["src/a.py"]
    tree.failures["landed_since"] = TreeFailed("git log failed: bad revision")
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    lane_of(session).planner.last_super_commit = "s1"
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session), 10)
    prompt = super_task(events).task.prompt
    assert prompt.endswith(f"at base\n\n{SAMPLED}\n- src/a.py")


async def test_successive_super_reviews_sample_the_files_sampled_longest_ago() -> None:
    tree = FakeTree()
    tree.tracked[tree.base] = TRACKED
    session, _, _ = session_for(
        *LENSES * 3,
        tree=tree,
        review=True,
        review_limits=ReviewLimits(super_sample=1),
    )
    sampled = {"src/a.py": 1, "src/b.cpp": 3}
    lane_of(session).planner.super_count = SuperReviewCount(super_reviews=3, sampled=sampled)
    events: list[Event] = []
    prompts: list[str] = []
    for _ in range(3):
        await send(session, FireTrigger(trigger=SUPER_REVIEW))
        events = await asyncio.wait_for(settle(session, events), 10)
        prompts.append(super_task(events).task.prompt.split(f"{SAMPLED}\n", 1)[1])
    assert prompts == ["- tests/t.py", "- src/a.py", "- src/b.cpp"]
    count = lane_of(session).planner.super_count
    assert count.super_reviews == 6
    assert count.sampled == {"src/a.py": 5, "src/b.cpp": 6, "tests/t.py": 4}


async def test_a_super_review_forgets_the_sampled_files_no_longer_tracked() -> None:
    tree = FakeTree()
    tree.tracked[tree.base] = ["src/a.py"]
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    lane_of(session).planner.super_count = SuperReviewCount(super_reviews=1, sampled={"gone.py": 1})
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    await asyncio.wait_for(settle(session), 10)
    assert lane_of(session).planner.sampled == {"src/a.py": 2}


async def test_a_super_review_whose_tracked_files_cannot_be_listed_keeps_the_record() -> None:
    tree = FakeTree()
    tree.failures["tracked_files"] = TreeFailed("git ls-tree failed: bad revision")
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    lane_of(session).planner.super_count = SuperReviewCount(super_reviews=1, sampled={"a.py": 1})
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    await asyncio.wait_for(settle(session), 10)
    count = lane_of(session).planner.super_count
    assert (count.super_reviews, count.sampled) == (2, {"a.py": 1})


async def test_a_super_review_is_skipped_when_the_base_commit_cannot_be_read() -> None:
    tree = FakeTree()
    tree.failures["base_commit"] = TreeFailed("git rev-parse failed: unknown revision")
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await asyncio.wait_for(settle(session, until=_errored), 10)
    message = "Super review skipped: git rev-parse failed: unknown revision"
    assert ErrorOccurred(code=GIT_FAILED, message=message) in events
    assert not _roles_seen(events) & set(SUPER_LENS_REVIEWERS)
    assert tree.snapshots == []


async def test_a_super_review_given_input_is_refused_and_none_starts() -> None:
    tree = FakeTree()
    session, _, _ = session_for(*LENSES, tree=tree, review=True)
    await send(session, FireTrigger(trigger=SUPER_REVIEW, input={"x": "1"}))
    events = await asyncio.wait_for(settle(session, until=_refused), 10)
    assert _refusals(events) == [refused(INVALID, "The super-review trigger takes no input.")]
    assert not _error_messages(events)
    assert not _roles_seen(events) & set(SUPER_LENS_REVIEWERS)
    assert tree.snapshots == []


async def test_without_reviews_a_super_review_is_refused() -> None:
    session, _, _ = session_for(tree=FakeTree())
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    events = await events_until(session, of_type(CommandRefused))
    message = "Super reviews need reviews turned on, inside a git repository."
    assert events[-1] == refused(NOT_CONFIGURED, message)


async def test_review_status_follows_batches_and_the_pause() -> None:
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"),),
        tree=FakeTree(),
        review=True,
        review_limits=ReviewLimits(rounds=1),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    statuses = [e for e in events if isinstance(e, ReviewStatus)]
    assert statuses[0] == ReviewStatus(paused=False, open_batches=1)
    assert statuses[-1] == ReviewStatus(paused=True, open_batches=0)
    assert all(a != b for a, b in itertools.pairwise(statuses))  # Only changes.


async def test_cancelling_a_reviewer_stops_its_review_and_the_lane_carries_on() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, launcher = session_for(
        claims_done, claims_done, reviewers=(endless, suggests()), tree=FakeTree(), review=True
    )
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await asyncio.wait_for(reviewing.wait(), 10)
    [reviewer] = reviewers_of(launcher)
    await send(session, CancelAgent(agent_id=reviewer))
    await asyncio.wait_for(collecting, 10)
    assert statuses_of(events, "reviewer") == ["cancelled", "completed"]
    stopped = next(t for t in board(events) if t.task.role == "reviewer")
    assert (stopped.reason, stopped.reason_detail) == (REVIEW_STOPPED_BY_YOU, "")
    assert AgentExited(agent_id=reviewer, reason="killed") in events
    assert statuses_of(events, "coder") == ["completed", "completed"]


async def test_a_review_cancelled_as_the_session_closes_is_just_cancelled() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(claims_done, reviewers=(endless,), tree=FakeTree(), review=True)
    await start(session)
    collecting = asyncio.ensure_future(settle(session, until=lambda _: reviewing.is_set()))
    await asyncio.wait_for(reviewing.wait(), 10)
    await asyncio.wait_for(collecting, 10)
    await session.close()
    [review] = [t for t in board(queued(session)) if t.task.role == "reviewer"]
    assert (review.status, review.reason, review.reason_detail) == ("cancelled", CANCELLED, "")


async def test_a_review_cancelled_as_the_session_closes_leaves_its_batch_open() -> None:
    reviewing = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(claims_done, reviewers=(endless,), tree=FakeTree(), review=True)
    await start(session)
    collecting = asyncio.ensure_future(settle(session, until=lambda _: reviewing.is_set()))
    await asyncio.wait_for(reviewing.wait(), 10)
    events = await asyncio.wait_for(collecting, 10)
    await session.close()
    statuses = [e for e in events + queued(session) if isinstance(e, ReviewStatus)]
    assert statuses[-1] == ReviewStatus(paused=False, open_batches=1)


async def test_a_review_ending_as_the_session_closes_starts_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewing, release = asyncio.Event(), asyncio.Event()

    async def waits_then_suggests(call: Call) -> TurnEndReason:
        await call("get_task")
        reviewing.set()
        await release.wait()
        return await suggests("Add tests")(call)

    close_turns = WorkTurns.close

    async def close_then_release(turns: WorkTurns) -> None:
        await close_turns(turns)
        release.set()
        await quiet_within(0.05)

    monkeypatch.setattr(WorkTurns, "close", close_then_release)
    session, _, launcher = session_for(
        claims_done, claims_done, reviewers=(waits_then_suggests,), tree=FakeTree(), review=True
    )
    await start(session)
    await asyncio.wait_for(reviewing.wait(), 10)
    await session.close()
    assert [la.agent.role for la in launcher.launches] == ["coder", "reviewer"]
    assert {t for t in asyncio.all_tasks() if not t.done()} == {asyncio.current_task()}


class _HeldTree(FakeTree):
    """A coder tree whose head, once work is committed, is read only after ``release``."""

    def __init__(self) -> None:
        super().__init__()
        self.heading, self.release = asyncio.Event(), asyncio.Event()

    async def head(self) -> str:
        if self.commits:
            self.heading.set()
            await self.release.wait()
        return await super().head()


async def test_a_task_landing_as_the_session_closes_starts_no_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = _HeldTree()
    close_lane = ReviewLane.close

    async def close_then_release(lane: ReviewLane) -> None:
        await close_lane(lane)
        tree.release.set()

    monkeypatch.setattr(ReviewLane, "close", close_then_release)
    session, _, launcher = session_for(claims_done, reviewers=(suggests(),), tree=tree, review=True)
    await start(session)
    await asyncio.wait_for(tree.heading.wait(), 10)
    await session.close()
    assert [la.agent.role for la in launcher.launches] == ["coder"]
    assert any(t.task.role == "reviewer" for t in board(queued(session)))
    assert not lane_of(session).running
    assert {t for t in asyncio.all_tasks() if not t.done()} == {asyncio.current_task()}


async def test_a_review_removed_as_it_is_followed_up_is_still_reported_as_a_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    following, release = asyncio.Event(), asyncio.Event()
    publish_status = ReviewLane._publish_status

    async def held_once_its_batch_closes(lane: ReviewLane) -> None:
        if not (lane.planner.open_batches or following.is_set()):
            following.set()
            await release.wait()
        await publish_status(lane)

    monkeypatch.setattr(ReviewLane, "_publish_status", held_once_its_batch_closes)
    session, _, _ = session_for(claims_done, reviewers=(suggests(),), tree=FakeTree(), review=True)
    await start(session)
    await asyncio.wait_for(following.wait(), 10)
    await send(session, RemoveTask(task_id="2"))
    assert reported(session.activity) == (Activity(kind="review"),)
    release.set()
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    assert [(t.id, t.status) for t in board(events)] == [("1", "completed")]


async def test_a_cancelled_reviewer_that_suggested_is_not_run_again() -> None:
    suggested, release = asyncio.Event(), asyncio.Event()

    async def suggests_then_waits(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        suggested.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def waits_then_done(call: Call) -> TurnEndReason:
        await release.wait()
        return await claims_done(call)

    session, _, launcher = session_for(
        claims_done,
        waits_then_done,
        reviewers=(suggests_then_waits, suggests()),
        tree=FakeTree(),
        review=True,
    )
    await start(session)
    await asyncio.wait_for(suggested.wait(), 10)
    [reviewer] = reviewers_of(launcher)
    await send(session, CancelAgent(agent_id=reviewer))
    await events_until(session, task_is("2", "cancelled"))
    await send(session, RunTasks())
    await quiet_within(0.05)
    release.set()
    events = await asyncio.wait_for(settle(session), 10)
    assert reviewers_of(launcher)[0] == reviewer
    assert [la.agent.task_id for la in launcher.launches].count("2") == 1
    assert [(t.task.role, t.status) for t in board(events)][:3] == [
        ("coder", "completed"),
        ("reviewer", "cancelled"),
        ("coder", "completed"),
    ]


async def test_cancelling_the_coder_is_like_escape() -> None:
    working = asyncio.Event()

    async def endless(call: Call) -> TurnEndReason:
        await call("get_task")
        working.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(endless)
    await start(session, "one", "two")
    await asyncio.wait_for(working.wait(), 5)
    await send(session, CancelAgent(agent_id="agent-1"))
    events = await asyncio.wait_for(
        settle(session, until=lambda ev: [t.status for t in board(ev)] == ["cancelled", "pending"]),
        10,
    )
    assert [(t.status, t.reason, t.reason_detail) for t in board(events)] == [
        ("cancelled", STOPPED_BY_YOU, ""),
        ("pending", "", ""),
    ]


async def test_cancelling_an_unknown_agent_is_reported() -> None:
    session, _, _ = session_for()
    await send(session, CancelAgent(agent_id="agent-7"))
    events = await events_until(session, of_type(CommandRefused))
    assert events[-1] == refused(UNKNOWN, "No running agent 'agent-7'.")


async def test_cancelling_a_review_that_is_not_running_does_nothing() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    lane_of(session).cancel("42")


async def test_a_review_can_suggest_after_its_first_suggestion_landed() -> None:
    suggested = asyncio.Event()
    replies: list[Reply] = []
    sessions: list[WorkspaceHost] = []

    async def waits_for_suggestion(call: Call) -> TurnEndReason:
        await suggested.wait()
        return await claims_done(call)

    async def suggests_after_landing(call: Call) -> TurnEndReason:
        await call("get_task")
        replies.append(await call("suggest_task", title="Add tests", prompt="Please."))
        suggested.set()
        await eventually(lambda: board_of(sessions[0]).get("4").status == "completed")
        await quiet_within(0.1)  # Let the work lane tell the review lane it landed.
        replies.append(await call("suggest_task", title="Rename x", prompt="Please."))
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        waits_for_suggestion,
        claims_done,
        claims_done,
        reviewers=(suggests_after_landing, suggests(), suggests()),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert [r.is_error for r in replies] == [False, False]
    shown = [(t.id, t.task.role, t.status, t.batch) for t in board(events)]
    assert shown == [
        ("1", "coder", "completed", None),
        ("2", "coder", "completed", None),
        ("3", "reviewer", "completed", None),
        ("4", "coder", "completed", "batch-1"),
        ("5", "reviewer", "completed", None),  # Of task 2.
        ("6", "coder", "completed", "batch-1"),
        ("7", "reviewer", "completed", None),
    ]
    batch_review = board(events)[6]
    assert batch_review.task.prompt.startswith("Review the 2 commits made for review batch-1")


async def test_a_snapshot_that_fails_errors_the_review_and_the_next_review_still_runs() -> None:
    tree = FakeTree()
    tree.failures["snapshot"] = TreeFailed("git worktree add failed: no space left")
    session, _, _ = session_for(
        claims_done, claims_done, reviewers=(suggests(),), tree=tree, review=True
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert statuses_of(events, "reviewer") == ["error", "completed"]
    assert statuses_of(events, "coder") == ["completed", "completed"]
    message = "git worktree add failed: no space left"
    assert ErrorOccurred(code=GIT_FAILED, message=message) in events
    errored = next(t for t in board(events) if t.status == "error")
    assert (errored.reason, errored.reason_detail) == (f"Error: {message}", message)
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0
    assert tree.snapshots == ["c2"] and tree.dropped == ["/work/review-c2"]


async def test_a_diff_that_cannot_be_shown_skips_the_review_and_the_run_goes_on() -> None:
    tree = FakeTree()
    tree.failures["show"] = TreeFailed("git show failed: bad object c1")
    session, _, _ = session_for(
        claims_done, claims_done, reviewers=(suggests(),), tree=tree, review=True
    )
    await start(session, "one", "two")
    events = await asyncio.wait_for(settle(session), 10)
    assert [(t.task.role, t.status) for t in board(events)] == [
        ("coder", "completed"),
        ("coder", "completed"),
        ("reviewer", "completed"),
    ]
    assert (
        ErrorOccurred(code=GIT_FAILED, message="Review skipped: git show failed: bad object c1")
        in events
    )
    assert "diff of c2" in board(events)[2].task.prompt


LANDED = "Landed since (check your findings against these):"


def review_task(events: list[Event]) -> TaskState:
    """The one review task last published in ``events``."""
    [review] = [t for t in board(events) if t.task.role == "reviewer"]
    return review


class LandingTree(FakeTree):
    """A coder tree that tells, of each commit, the subjects of the commits merged after it,
    each only once it has merged."""

    async def integrate(self) -> Merge:
        merge = await super().integrate()
        subject = self.commits[-1].partition("\n")[0]
        for subjects in self.since.values():
            subjects.append(subject)
        self.since[await self.head()] = []
        return merge


def lands(subject: str) -> Script:
    """An agent that claims its task done, so its commit's subject is ``subject``."""

    async def work(call: Call) -> TurnEndReason:
        return await claims_done(call, subject)

    return work


def prompts_of(task_id: str, events: list[Event]) -> list[str]:
    """Task ``task_id``'s prompt each time ``events`` published the board with it."""
    changes = [e for e in events if isinstance(e, TasksChanged)]
    return [t.task.prompt for e in changes for t in e.tasks if t.id == task_id]


async def test_a_review_is_told_the_subjects_that_landed_before_it_started_oldest_first() -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    briefs: list[str] = []

    async def waits_then_reviews(call: Call) -> TurnEndReason:
        started.set()
        await gate.wait()
        return await suggests()(call)

    session, _, _ = session_for(
        *(lands(f"Add {letter}") for letter in "ABCD"),
        reviewers=(waits_then_reviews, suggests(briefs=briefs), suggests(), suggests()),
        tree=LandingTree(),
        review=True,
        review_limits=ONE_AT_A_TIME,
    )
    await start(session, "one", "two", "three", "four")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: [t.task.role for t in latest_board(events)].count("reviewer") == 4)
    await started.wait()  # The review of c1 is held back, so that of c2 starts after D landed.
    gate.set()
    await asyncio.wait_for(collecting, 10)
    [brief] = briefs
    assert brief.startswith("Review commit c2")
    assert f"{LANDED}\n- Add C\n- Add D\n\ndiff of c2" in brief
    second = [t for t in board(events) if t.task.role == "reviewer"][1]
    prompts = prompts_of(second.id, events)
    assert LANDED not in prompts[0]  # As it was queued, before C and D landed.
    assert prompts[-1] == brief.split("\n\nWhen you have finished", 1)[0]


async def test_a_review_with_nothing_landed_before_it_started_is_told_of_none() -> None:
    briefs: list[str] = []
    tree = LandingTree()
    session, _, _ = session_for(
        lands("Add A"), reviewers=(suggests(briefs=briefs),), tree=tree, review=True
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    [brief] = briefs
    assert brief.startswith("Review commit c1") and "Landed since" not in brief
    assert "Landed since" not in review_task(events).task.prompt
    assert tree.asked_since == [("c1", None)]


async def test_a_batch_review_is_told_what_landed_since_its_last_commit() -> None:
    tree = FakeTree()
    tree.since["c3"] = ["Fix y"]
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests", "Rename x"), suggests()),
        tree=tree,
        review=True,
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    batch_review = board(events)[-1]
    assert f"{LANDED}\n- Fix y\n\n" in batch_review.task.prompt
    assert tree.asked_since == [("c1", None), ("c3", None)]


async def test_a_review_of_a_commit_samples_no_files() -> None:
    tree = FakeTree()
    tree.tracked["c1"] = TRACKED
    session, _, _ = session_for(claims_done, reviewers=(suggests(),), tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    prompt = review_task(events).task.prompt
    assert SAMPLED not in prompt and "src/a.py" not in prompt


async def test_a_review_whose_landed_commits_cannot_be_listed_as_it_starts_runs_without() -> None:
    briefs: list[str] = []
    tree = FakeTree()
    tree.since["c1"] = ["Add B"]
    tree.failures["landed_since"] = TreeFailed("git log failed: bad revision")
    session, _, _ = session_for(
        claims_done, reviewers=(suggests(briefs=briefs),), tree=tree, review=True
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    review = review_task(events)
    assert review.status == "completed" and "diff of c1" in review.task.prompt
    assert "Landed since" not in review.task.prompt
    [brief] = briefs
    assert "diff of c1" in brief and "Landed since" not in brief
    # As it started, up to the base's head: it's not asked as a review is queued.
    assert tree.asked_since == [("c1", None)]
    assert not _errored(events)


def reviewed_as(template: str) -> dict[str, Trigger]:
    """The shipped triggers, the review's template replaced whole by ``template``, as a project
    may replace it."""
    return {**TRIGGERS, "review": TRIGGERS["review"].model_copy(update={"prompt": template})}


async def reviewed_from(template: str, *landed: str) -> tuple[TaskState, str]:
    """The review task, as it ended, and the brief of the one review of a commit queued from the
    review trigger's ``template``, the commits with subjects ``landed`` merged after it."""
    briefs: list[str] = []
    tree = FakeTree()
    tree.since["c1"] = list(landed)
    session, _, _ = session_for(
        claims_done,
        reviewers=(suggests(briefs=briefs),),
        tree=tree,
        review=True,
        triggers=reviewed_as(template),
    )
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    [brief] = briefs
    return review_task(events), brief.split("\n\nWhen you have finished", 1)[0]


async def test_a_project_review_template_of_one_paragraph_starts_and_keeps_its_text() -> None:
    review, brief = await reviewed_from("Review {commit}.", "Add B")
    assert (review.status, review.task.prompt, brief) == ("completed", "Review c1.", "Review c1.")


async def test_a_project_review_template_in_another_order_is_not_spliced_into() -> None:
    template = "{diff}\n\nYou may make at most {cap} suggestions.\n\n{title}"
    review, brief = await reviewed_from(template, "Add B")
    expected = "diff of c1\n\nYou may make at most 5 suggestions.\n\nReview commit c1"
    assert (review.task.prompt, brief) == (expected, expected)


async def test_a_project_review_template_lists_what_landed_since_as_the_review_starts() -> None:
    review, brief = await reviewed_from("Review {commit}.\n\n{landed_since}", "Add B", "Add C")
    expected = f"Review c1.\n\n{LANDED}\n- Add B\n- Add C"
    assert (review.task.prompt, brief) == (expected, expected)


async def test_a_project_review_triggers_review_cant_be_edited_and_starts_as_queued() -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    briefs: list[str] = []

    async def waits_then_reviews(call: Call) -> TurnEndReason:
        started.set()
        await gate.wait()
        return await suggests()(call)

    # A project's review trigger of its own, whose reviews are booked as the shipped one's are.
    check = TRIGGERS["review"].model_copy(update={"prompt": "Review {commit}.\n\n{landed_since}"})
    off = TRIGGERS["review"].model_copy(update={"enabled": False})
    tree = FakeTree()
    tree.since["c2"] = ["Add C"]
    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(waits_then_reviews, suggests(briefs=briefs)),
        tree=tree,
        review=True,
        triggers={**TRIGGERS, "review": off, "check": check},
        review_limits=ONE_AT_A_TIME,
    )
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: [t.task.role for t in latest_board(events)].count("reviewer") == 2)
    await started.wait()  # The review of c1 is held back, so that of c2 waits to start.
    edited = TaskSpec(prompt="Look closely at c2.", role="reviewer")
    await send(session, EditTask(task_id="4", task=edited))
    gate.set()
    await asyncio.wait_for(collecting, 10)
    assert refused(INVALID, "Reviews can't be edited; remove it instead.") in events
    expected = f"Review c2.\n\n{LANDED}\n- Add C"
    assert board(events)[3].task.prompt == expected
    [brief] = briefs
    assert brief.split("\n\nWhen you have finished", 1)[0] == expected


async def test_an_unexpected_error_in_the_review_lane_is_reported() -> None:
    tree = FakeTree()
    tree.failures["snapshot"] = ValueError("the lane tripped")
    session, _, _ = session_for(claims_done, tree=tree, review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    message = "The review lane stopped on an unexpected error: the lane tripped"
    assert ErrorOccurred(code=INTERNAL, message=message) in events
    [review] = [t for t in board(events) if t.task.role == "reviewer"]
    assert (review.status, review.reason, review.reason_detail) == (
        "error",
        BUG,
        "the lane tripped",
    )
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_a_suggestion_removed_from_its_batch_leaves_the_rest_reviewed() -> None:
    sessions: list[WorkspaceHost] = []

    async def suggests_then_one_is_removed(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please.")
        await call("suggest_task", title="Rename x", prompt="Please.")
        await send(sessions[0], RemoveTask(task_id="4"))
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        claims_done,
        reviewers=(suggests_then_one_is_removed, suggests()),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    assert [(t.id, t.task.role, t.status) for t in board(events)] == [
        ("1", "coder", "completed"),
        ("2", "reviewer", "completed"),
        ("3", "coder", "completed"),
        ("5", "reviewer", "completed"),
    ]
    assert board(events)[3].task.prompt.startswith("Review the 1 commits made for review batch-1")
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_a_queued_review_that_is_removed_leaves_no_batch_open() -> None:
    gate = asyncio.Event()

    async def slow(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Reviewed")
        return "completed"

    session, _, launcher = session_for(
        claims_done,
        claims_done,
        reviewers=(slow,),
        tree=FakeTree(),
        review=True,
        review_limits=ONE_AT_A_TIME,
    )
    await start(session, "one", "two")
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: [t.task.role for t in latest_board(events)].count("reviewer") >= 2)
    await send(session, RemoveTask(task_id="4"))
    gate.set()
    await asyncio.wait_for(collecting, 10)
    assert [(t.id, t.task.role) for t in board(events)] == [
        ("1", "coder"),
        ("2", "coder"),
        ("3", "reviewer"),
    ]
    assert len(reviewers_of(launcher)) == 1
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_a_finished_review_can_be_removed_but_not_edited() -> None:
    session, _, _ = session_for(claims_done, reviewers=(suggests(),), tree=FakeTree(), review=True)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await send(session, EditTask(task_id="2", task=TaskSpec(prompt="Again", role="reviewer")))
    await send(session, RemoveTask(task_id="2"))
    events = await asyncio.wait_for(settle(session, events), 10)
    assert refused(INVALID, "Reviews can't be edited; remove it instead.") in events
    assert [(t.id, t.status) for t in board(events)] == [("1", "completed")]
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def edit_a_user_review(session: WorkspaceHost) -> list[Event]:
    """The events after adding a reviewer task to ``session`` and editing its prompt."""
    await send(session, AddTask(task=TaskSpec(prompt="Review the parser", role="reviewer")))
    await send(session, EditTask(task_id="1", task=TaskSpec(prompt="Again", role="reviewer")))
    events = queued(session)
    await session.close()
    return events


async def test_a_review_the_user_queued_can_be_edited_without_the_review_lane() -> None:
    session, _, _ = session_for()
    events = await edit_a_user_review(session)
    assert not _error_messages(events)
    assert [t.task.prompt for t in board(events)] == ["Again"]


async def test_a_review_the_user_queued_can_be_edited_with_the_review_lane() -> None:
    session, _, _ = session_for(tree=FakeTree(), review=True)
    events = await edit_a_user_review(session)
    assert not _error_messages(events)
    assert [t.task.prompt for t in board(events)] == ["Again"]


async def test_a_task_edited_back_to_pending_is_reviewed_with_its_batch() -> None:
    sessions: list[WorkspaceHost] = []

    async def reopens_task_3(call: Call) -> TurnEndReason:
        await call("get_task")
        await send(sessions[0], EditTask(task_id="3", task=TaskSpec(prompt="Add tests, again.")))
        await call("complete_task", summary="Did it")
        return "completed"

    session, _, _ = session_for(
        claims_done,
        gives_up,
        reopens_task_3,
        claims_done,
        reviewers=(suggests("Add tests", "Rename x"), suggests()),
        tree=FakeTree(),
        review=True,
    )
    sessions.append(session)
    await start(session)
    events = await asyncio.wait_for(settle(session, until=lambda e: len(board(e)) == 4), 10)
    assert [t.status for t in board(events)][2:] == ["pending", "completed"]
    await send(session, RunTasks())
    events = await asyncio.wait_for(settle(session, events), 10)
    assert [(t.id, t.task.role, t.status) for t in board(events)] == [
        ("1", "coder", "completed"),
        ("2", "reviewer", "completed"),
        ("3", "coder", "completed"),
        ("4", "coder", "completed"),
        ("5", "reviewer", "completed"),
    ]
    prompt = board(events)[4].task.prompt
    assert prompt.startswith("Review the 2 commits made for review batch-1")
    assert "ended without landing a commit" not in prompt
    assert [e for e in events if isinstance(e, ReviewStatus)][-1].open_batches == 0


async def test_closing_the_session_mid_suggestion_leaves_its_batch_as_it_was() -> None:
    working = asyncio.Event()

    async def works_forever(call: Call) -> TurnEndReason:
        await call("get_task")
        working.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session, _, _ = session_for(
        claims_done, works_forever, reviewers=(suggests("Add tests"),), tree=FakeTree(), review=True
    )
    await start(session)
    await asyncio.wait_for(working.wait(), 10)
    lane = lane_of(session)
    before = lane.planner.snapshot()
    await session.close()
    assert lane.planner.snapshot() == before
    roles = [t.task.role for t in board_of(session).tasks]
    assert roles == ["coder", "reviewer", "coder"]
