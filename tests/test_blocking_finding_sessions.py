"""Blocking findings holding merges in a session: a merge pass filing a blocking task holds them,
its fix runs first, in a batch of its own, and lands while the work behind it waits; the review
of that batch by the raising lens lifts the hold, files the next fix, or, past the fixes allowed
or with no review to check it, leaves the user to step in; and a blocking task with no blocking
finding among its findings is refused."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from orbit.agent import Verification, WorkspaceHost
from orbit.agent.application.ports import Reply
from orbit.agent.domain.merge_holds import (
    FindingHold,
    finding_resumed_note,
    finding_step_in_note,
    note_finding_held,
    unchecked_note,
)
from orbit.agent.domain.merge_pass import NOT_BLOCKING
from orbit.agent.domain.reviews import ReviewLimits, pause_notice
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
from orbit.protocol import BUGS_REVIEWER, REVIEWER, MergeHold, TaskState, TurnEndReason
from tests.agent_fakes import (
    LINT,
    ROLES,
    TRIGGERS,
    Call,
    Script,
    board_of,
    gives_up,
    holds_of,
    session_for,
    snapshot_of,
    start,
    with_orbit_tools,
    work_lane_of,
)
from tests.merge_hold_helpers import LandingTree, Seen, SetSteps
from tests.waiting import eventually

TESTS = "tests-reviewer"
LENS_TOOLS = (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP, REPORT_FINDING)
MERGE_TOOLS = (GET_TASK, COMPLETE_TASK, REPORT_PROGRESS, GIVE_UP, SUGGEST_TASK, DISMISS_FINDING)


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


ROLES_WITH_LENSES = {
    **ROLES,
    BUGS_REVIEWER: lens(blocking=True),
    TESTS: lens(blocking=False),
    REVIEWER: with_orbit_tools(ROLES[REVIEWER], MERGE_TOOLS),
}
"""The built-in roles, with a bugs lens, which may report blocking findings, a tests lens, and a
reviewer that merges their findings."""


def lens_trigger(role: str, **options: Any) -> Trigger:
    """A trigger queuing ``role``'s look at each commit ``review-rounds`` finds due."""
    prompt = f"Look at {{commit}} as {role}."
    return Trigger(on="task_landed", role=role, prompt=prompt, guards=("review-rounds",), **options)


OFF = {
    name: TRIGGERS[name].model_copy(update={"enabled": False}) for name in ("review", "merge-pass")
}
LENS_TRIGGERS = {
    **TRIGGERS,
    **OFF,
    "bugs": lens_trigger(BUGS_REVIEWER),
    "tests": lens_trigger(TESTS),
    "merge": Trigger(
        on="reviews_finished",
        role=REVIEWER,
        prompt="{title}\n\n{findings}",
        when={"findings": "some"},
    ),
}
"""A bugs and a tests lens reviewing each commit and batch, and a merge pass on their
findings."""

Act = Callable[[Call], Awaitable[None]]
"""What an agent does with its task once it has fetched it, before it claims it done."""


class Cast:
    """Agents acting on their tasks by prompt, whichever lane launches them first: each fetches
    its task and plays the next act of the first cue among ``acts`` its prompt holds with acts
    left, then claims it done."""

    def __init__(self, acts: dict[str, list[Act]]) -> None:
        self.acts = {cue: list(each) for cue, each in acts.items()}

    async def __call__(self, call: Call) -> TurnEndReason:
        brief = (await call("get_task")).text
        cue = next(cue for cue, acts in self.acts.items() if cue in brief and acts)
        await self.acts[cue].pop(0)(call)
        await call("complete_task", summary="done")
        return "completed"


def finds(*findings: tuple[str, str], gate: asyncio.Event | None = None) -> Act:
    """A lens reporting ``findings``, each a title and severity, once ``gate``, if any, is
    set."""

    async def act(call: Call) -> None:
        if gate is not None:
            await gate.wait()
        for title, severity in findings:
            await call("report_finding", title=title, body="Why.", severity=severity)

    return act


async def works(call: Call) -> None:
    """A coder doing its task."""


def files(*filings: tuple[str, str, str], replies: list[Reply] | None = None) -> Script:
    """A merge pass filing ``filings``, each a title, the one finding it files and its
    severity, recording orbit's replies in ``replies``, if given."""

    async def merge(call: Call) -> TurnEndReason:
        await call("get_task")
        for title, finding, severity in filings:
            prompt = f"Please {title.lower()}."
            reply = await call(
                "suggest_task", title=title, prompt=prompt, findings=[finding], severity=severity
            )
            if replies is not None:
                replies.append(reply)
                if reply.is_error:
                    await call("dismiss_finding", finding=finding, reason="Refused.")
        await call("complete_task", summary="Merged")
        return "completed"

    return merge


def status(session: WorkspaceHost, task_id: str) -> str:
    """Task ``task_id``'s status in ``session``."""
    return board_of(session).get(task_id).status


def held_session(
    cast: Cast, *merges: Script, fallback_fixes: int = 3, **options: Any
) -> WorkspaceHost:
    """A session whose sets hold no merges, reviewing what lands by lens, its agents played by
    ``cast`` and its merge passes by ``merges``, its findings' holds queuing ``fallback_fixes``
    fixes each; ``options`` go to :func:`session_for`."""
    defaults: dict[str, Any] = {
        "roles": ROLES_WITH_LENSES,
        "triggers": LENS_TRIGGERS,
        "review_limits": ReviewLimits(rounds=3),
    }
    verification = Verification((LINT,), SetSteps(), fallback_fixes=fallback_fixes)
    session, _, _ = session_for(
        *[cast] * 20,
        reviewers=merges,
        verification=verification,
        tree=LandingTree(),
        review=True,
        **(defaults | options),
    )
    return session


CRASH = ("Crash on empty input", "blocking")
NO_TEST = ("No test for it", "normal")
HELD = note_finding_held(FindingHold(BUGS_REVIEWER, "c1", "Fix the crash", "", None))
VIEW = MergeHold(set=BUGS_REVIEWER, commit="c1", reason="blocking finding", finding="Fix the crash")


def titled(session: WorkspaceHost, title: str) -> list[TaskState]:
    """The tasks on ``session``'s board titled ``title``."""
    return [state for state in board_of(session).tasks if state.task.prompt.startswith(title)]


def waiting(session: WorkspaceHost) -> tuple[str, ...]:
    """The tasks whose work waits for the holds in ``session``."""
    work = work_lane_of(session)
    assert work is not None
    return work.waiting_to_merge


async def run(session: WorkspaceHost, seen: Seen) -> None:
    """Start ``session`` on one task, "Add A.", until a blocking finding holds merges."""
    await start(session, "Add A.")
    await eventually(lambda: HELD in seen.notices())


async def test_a_blocking_task_holds_merges_runs_first_and_its_clean_review_lifts_the_hold() -> (
    None
):
    reviewed = asyncio.Event()
    cast = Cast(
        {
            "Add A": [works],
            "as bugs-reviewer": [finds(CRASH), finds(gate=reviewed), finds()],
            "as tests-reviewer": [finds(NO_TEST), finds(), finds()],
            "Please fix the crash": [works],
            "Please add a test": [works],
        }
    )
    merge = files(("Fix the crash", "F1", "blocking"), ("Add a test", "F2", "normal"))
    session = held_session(cast, merge)
    seen = Seen(session)
    await run(session, seen)

    [fix], [later] = titled(session, "Fix the crash"), titled(session, "Add a test")
    assert board_of(session).tasks[0] == fix
    assert (fix.batch, later.batch) == ("batch-2", "batch-1")
    await eventually(lambda: waiting(session) == (later.id,))
    shown = await snapshot_of(session)
    assert shown.merge_holds == (VIEW,)
    assert seen.merged() == [("1", "c1"), (fix.id, "c2")]

    reviewed.set()
    await eventually(lambda: len(seen.merged()) == 3)
    shown = await snapshot_of(session)
    seen.close()
    await session.close()
    assert seen.merged()[2] == (later.id, "c3")
    assert seen.notices() == [HELD, finding_resumed_note(BUGS_REVIEWER)]
    assert (shown.merge_holds, shown.waiting_to_merge) == ((), ())


async def test_a_blocking_finding_its_fixs_merge_pass_gives_up_on_keeps_merges_held() -> None:
    cast = Cast(
        {
            "Add A": [works],
            "as bugs-reviewer": [finds(CRASH), finds(CRASH), finds()],
            "as tests-reviewer": [finds(NO_TEST), finds(), finds()],
            "Please fix the crash": [works],
            "Please add a test": [works],
        }
    )
    merge = files(("Fix the crash", "F1", "blocking"), ("Add a test", "F2", "normal"))
    session = held_session(cast, merge, gives_up)
    seen = Seen(session)
    await run(session, seen)
    await eventually(lambda: unchecked_note(BUGS_REVIEWER) in seen.notices())
    [fix], [later] = titled(session, "Fix the crash"), titled(session, "Add a test")
    holds = holds_of(session)
    shown = await snapshot_of(session)
    seen.close()
    await session.close()

    assert holds is not None and holds.held and holds.checking == ()
    assert seen.notices() == [HELD, unchecked_note(BUGS_REVIEWER)]
    assert seen.merged() == [("1", "c1"), (fix.id, "c2")]
    assert (shown.merge_holds, shown.waiting_to_merge) == ((VIEW,), (later.id,))


async def test_a_blocking_task_filed_again_is_the_next_fix_then_past_the_limit_a_normal_one() -> (
    None
):
    cast = Cast(
        {
            "Add A": [works],
            "as bugs-reviewer": [finds(CRASH), finds(CRASH), finds(CRASH)],
            "as tests-reviewer": [finds(), finds(), finds()],
            "Fix the crash": [works, works, works],
        }
    )
    titles = ("Fix the crash", "Fix the crash again", "Fix the crash once more")
    merges = [files((title, "F1", "blocking")) for title in titles]
    session = held_session(cast, *merges, fallback_fixes=2)
    seen = Seen(session)
    await run(session, seen)
    step_in = finding_step_in_note(BUGS_REVIEWER, 2)
    await eventually(lambda: step_in in seen.notices())
    [first], [again], [normal] = (titled(session, f"{title}\n") for title in titles)
    await eventually(lambda: waiting(session) == (normal.id,))
    shown = await snapshot_of(session)
    order = [state.id for state in board_of(session).tasks]
    seen.close()
    await session.close()

    assert seen.notices() == [HELD, step_in]
    assert order[:2] == [again.id, first.id]  # Each fix went first; the last filed, last.
    assert order[-1] == normal.id
    assert [(t.batch, t.round) for t in (first, again, normal)] == [
        ("batch-2", 1),
        ("batch-4", 2),
        ("batch-5", 3),
    ]
    assert [merged for merged, _ in seen.merged()] == ["1", first.id, again.id]
    assert shown.merge_holds == (VIEW,)


async def test_a_fix_whose_batch_is_at_the_last_round_cant_be_checked_and_stays_held() -> None:
    cast = Cast(
        {
            "Add A": [works],
            "as bugs-reviewer": [finds(CRASH)],
            "as tests-reviewer": [finds()],
            "Please fix the crash": [works],
        }
    )
    session = held_session(
        cast, files(("Fix the crash", "F1", "blocking")), review_limits=ReviewLimits(rounds=1)
    )
    seen = Seen(session)
    await run(session, seen)
    await eventually(lambda: unchecked_note(BUGS_REVIEWER) in seen.notices())
    holds = holds_of(session)
    [fix] = titled(session, "Fix the crash")
    seen.close()
    await session.close()

    assert holds is not None and holds.held
    assert seen.notices() == [HELD, pause_notice(1), unchecked_note(BUGS_REVIEWER)]
    assert [merged for merged, _ in seen.merged()] == ["1", fix.id]


async def test_a_fix_whose_lens_doesnt_review_batches_cant_be_checked_and_stays_held() -> None:
    cast = Cast(
        {
            "Add A": [works],
            "as bugs-reviewer": [finds(CRASH)],
            "as tests-reviewer": [finds(), finds()],
            "Please fix the crash": [works],
        }
    )
    commits_only = lens_trigger(BUGS_REVIEWER, when={"review": "commit"})
    session = held_session(
        cast,
        files(("Fix the crash", "F1", "blocking")),
        triggers={**LENS_TRIGGERS, "bugs": commits_only},
    )
    seen = Seen(session)
    await run(session, seen)
    await eventually(lambda: unchecked_note(BUGS_REVIEWER) in seen.notices())
    seen.close()
    await session.close()

    assert seen.notices() == [HELD, unchecked_note(BUGS_REVIEWER)]


async def test_a_fix_that_gives_up_cant_be_checked_and_stays_held() -> None:
    async def gives_up(call: Call) -> None:
        await call("give_up", reason="blocked")

    cast = Cast(
        {
            "Add A": [works],
            "as bugs-reviewer": [finds(CRASH)],
            "as tests-reviewer": [finds()],
            "Please fix the crash": [gives_up],
        }
    )
    session = held_session(cast, files(("Fix the crash", "F1", "blocking")))
    seen = Seen(session)
    await run(session, seen)
    await eventually(lambda: unchecked_note(BUGS_REVIEWER) in seen.notices())
    seen.close()
    await session.close()

    assert seen.notices() == [HELD, unchecked_note(BUGS_REVIEWER)]


async def test_a_blocking_task_whose_findings_are_all_normal_is_refused() -> None:
    replies: list[Reply] = []
    cast = Cast(
        {"Add A": [works], "as bugs-reviewer": [finds(NO_TEST)], "as tests-reviewer": [finds()]}
    )
    session = held_session(cast, files(("Add a test", "F1", "blocking"), replies=replies))
    await start(session, "Add A.")
    await eventually(lambda: len(replies) == 1)
    await eventually(lambda: status(session, "4") == "completed")
    holds = holds_of(session)
    await session.close()

    assert [(reply.text, reply.is_error) for reply in replies] == [(NOT_BLOCKING, True)]
    assert holds is not None and not holds.held
