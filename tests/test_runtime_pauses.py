"""Pauses per runtime: a usage limit pauses the tasks of the runtime whose agent hit it, until the
reset its failure states, else the reset its runtime's usage shows, else the retry, and an
outage's backoff is its runtime's own. The work lane keeps its order and waits; the review lane
runs its tasks on other runtimes meanwhile, and those it skipped once the pause ends."""

import asyncio
from datetime import timedelta
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.domain.roles import Role
from orbit.agent.domain.runtimes import CLAUDE, Failure
from orbit.agent.domain.usage import UsageReport
from orbit.agent.domain.usage_limits import outage_paused_note, paused_note
from orbit.protocol import (
    CODER,
    PAUSED_FOR_USAGE_LIMIT,
    REVIEWER,
    SUPER_REVIEWER,
    AddTask,
    Event,
    Notice,
    RemoveTask,
    RunTasks,
    SessionStateChanged,
    TaskSpec,
    TaskStatus,
    TurnEndReason,
    UsageLimit,
)
from tests.agent_fakes import (
    ROLES,
    START,
    Call,
    FakeHost,
    FakeTree,
    Script,
    ScriptedLauncher,
    Source,
    board_of,
    claims_done,
    events_until,
    idle,
    run,
    send,
    session_for,
    settled,
    turns_of,
)
from tests.connection_helpers import until

FAKE = "fake"
"""A runtime other than Claude, played by a launcher of its own."""

LIMIT = "You've hit your session limit · resets 6pm (UTC)"

QUOTA = "Quota spent"
"""The fake runtime's words for its usage limit."""

OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'

DRAFTER = Role(instructions="Draft it.", verify=False, runtime=FAKE)
"""A role in the work lane, on the fake runtime."""


class Waits:
    """A pause whose clock stands at :data:`START` and whose sleeps, noted, last until
    released."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await self.release.wait()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: START, sleep=self.sleep)


async def errs(call: Call) -> TurnEndReason:
    return "error"


def limited(
    fake: ScriptedLauncher, waits: Waits, *scripts: Script, **options: Any
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose roles on the fake runtime run through ``fake`` and whose roles on Claude
    run through the launcher returned, its first agent hitting Claude's usage limit, then
    playing ``scripts``, timed by ``waits``; ``options`` go to :func:`session_for`."""
    session, _, claude = session_for(
        errs, *scripts, host=fake.host, launchers={FAKE: fake}, pause=waits.pause, **options
    )
    claude.turn_errors.append(LIMIT)
    return session, claude


def roles_of(launcher: ScriptedLauncher) -> list[str]:
    return [launch.agent.role for launch in launcher.launches]


def statuses_of(session: WorkspaceHost) -> list[TaskStatus]:
    return [state.status for state in board_of(session).tasks]


def notes(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, Notice)]


def paused(event: Event) -> bool:
    """Whether ``event`` is a state change saying the run is paused."""
    return isinstance(event, SessionStateChanged) and event.paused_until is not None


async def test_the_work_lane_waits_out_claudes_pause_with_a_task_on_another_runtime_next() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), claims_done)
    roles = {**ROLES, "drafter": DRAFTER}
    session, claude = limited(fake, waits, claims_done, roles=roles)
    await run(session, TaskSpec(prompt="Code it."), TaskSpec(prompt="Draft it.", role="drafter"))
    await turns_of(session).wake()
    await settled()
    assert (roles_of(fake), statuses_of(session)) == ([], ["error", "pending"])

    waits.release.set()
    await events_until(session, idle())
    await session.close()

    assert (roles_of(claude), roles_of(fake)) == ([CODER, CODER], ["drafter"])
    assert statuses_of(session) == ["completed", "completed"]


async def test_the_review_lane_runs_another_runtimes_review_in_a_pause_and_claudes_after() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), reviewers=(claims_done,))
    roles = {**ROLES, SUPER_REVIEWER: ROLES[SUPER_REVIEWER].model_copy(update={"runtime": FAKE})}
    reviewers = (claims_done, claims_done)  # The skipped review, then the restarted coder's.
    options = {"reviewers": reviewers, "tree": FakeTree(), "review": True, "roles": roles}
    session, claude = limited(fake, waits, claims_done, **options)
    await run(session, TaskSpec(prompt="Code it."))
    await send(session, AddTask(task=TaskSpec(prompt="Review it.", role=REVIEWER)))
    await send(session, AddTask(task=TaskSpec(prompt="Review it all.", role=SUPER_REVIEWER)))
    await events_until(session, idle("review"))
    assert (roles_of(claude), roles_of(fake)) == ([CODER], [SUPER_REVIEWER])
    assert statuses_of(session) == ["error", "pending", "completed"]

    waits.release.set()
    await events_until(session, idle())  # The coder's work landed, so a review of it is queued.
    await events_until(session, idle("review"))
    await session.close()

    assert sorted(roles_of(claude)) == [CODER, CODER, REVIEWER, REVIEWER]
    assert statuses_of(session) == ["completed", "completed", "completed", "completed"]


async def test_a_limit_whose_failure_states_its_reset_pauses_the_run_until_then() -> None:
    waits = Waits()
    stated, shown = START + timedelta(hours=3), START + timedelta(hours=2)
    usage = Source(UsageReport((UsageLimit(label="Session", percent=100, resets_at=shown),)))
    session, _, launcher = session_for(errs, claims_done, pause=waits.pause, usage=usage)
    launcher.turn_errors.append(QUOTA)
    launcher.failures[QUOTA] = Failure("usage_limit", resets_at=stated)
    events = await asyncio.wait_for(run(session), 5)
    waits.release.set()
    await events_until(session, idle())
    await session.close()

    assert notes(events)[-1] == paused_note(stated, CLAUDE)
    assert waits.slept == [(stated - START).total_seconds()]


async def test_a_limit_on_another_runtime_isnt_timed_by_claudes_usage() -> None:
    waits = Waits()
    shown = START + timedelta(hours=2)
    usage = Source(UsageReport((UsageLimit(label="Session", percent=100, resets_at=shown),)))
    fake = ScriptedLauncher(FakeHost(), errs)
    fake.turn_errors.append(QUOTA)
    fake.failures[QUOTA] = Failure("usage_limit")
    roles = {**ROLES, CODER: ROLES[CODER].model_copy(update={"runtime": FAKE})}
    session, _, _ = session_for(
        host=fake.host, roles=roles, launchers={FAKE: fake}, pause=waits.pause, usage=usage
    )
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    events = await until(session, paused)
    await session.close()

    retry = START + timedelta(minutes=15)
    assert notes(events)[-1] == paused_note(retry, FAKE)
    state = events[-1]
    assert isinstance(state, SessionStateChanged)
    assert (state.paused_until, state.paused_for, state.paused_runtime) == (
        retry,
        PAUSED_FOR_USAGE_LIMIT,
        FAKE,
    )


async def test_an_agent_on_another_runtime_finishing_leaves_claudes_backoff_going() -> None:
    fake = ScriptedLauncher(FakeHost(), claims_done)
    roles = {**ROLES, "drafter": DRAFTER}
    options: dict[str, Any] = {"host": fake.host, "roles": roles, "launchers": {FAKE: fake}}
    session, _, claude = session_for(errs, errs, pause=Waits().pause, **options)
    claude.turn_errors.extend([OVERLOADED, OVERLOADED])
    drafted = TaskSpec(prompt="Draft it.", role="drafter")
    await run(session, TaskSpec(prompt="Code it."), drafted, TaskSpec(prompt="Code more."))

    await send(session, RemoveTask(task_id="1"))
    await send(session, RunTasks())
    events = await events_until(session, idle())
    await session.close()

    assert (roles_of(fake), statuses_of(session)) == (["drafter"], ["completed", "error"])
    assert notes(events)[-1] == outage_paused_note(START + timedelta(minutes=5), OVERLOADED, CLAUDE)
