"""Pauses per runtime: a usage limit pauses the tasks of the runtime whose agent hit it, until the
reset its failure states, else the reset its runtime's usage shows, else the retry, and an
outage's backoff is its runtime's own. The work lane keeps its order and waits; the review lane
runs its tasks on other runtimes meanwhile, and those it skipped once the pause ends. A review
whose agent hits its runtime's limit pauses that runtime just as the work lane's would, and runs
again once the pause ends; while one pause is in force, another runtime's is skipped instead."""

import asyncio
from datetime import timedelta
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.domain.roles import Role
from orbit.agent.domain.runtimes import CLAUDE, Failure
from orbit.agent.domain.usage import UsageReport
from orbit.agent.domain.usage_limits import (
    RESUMING_NOTE,
    WAIT_CANCELLED_NOTE,
    outage_paused_note,
    paused_note,
    tasks_resuming_note,
)
from orbit.protocol import (
    CODER,
    PAUSED_FOR_USAGE_LIMIT,
    REVIEWER,
    SUPER_REVIEWER,
    AddTask,
    CancelTurn,
    Event,
    Notice,
    RemoveTask,
    ResumeStorage,
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
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    Source,
    Stop,
    board,
    board_of,
    claims_done,
    events_until,
    idle,
    payloads,
    publish,
    run,
    send,
    session_for,
    settle,
    settled,
    suggests,
    turns_of,
)
from tests.connection_helpers import quiet, until

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


def reviewing(
    waits: Waits, *reviewers: Script, **options: Any
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose reviews run through ``reviewers``, its work lane idle, timed by
    ``waits``, with the launcher playing Claude; ``options`` go to :func:`session_for`."""
    every: dict[str, Any] = {"tree": FakeTree(), "review": True, "pause": waits.pause}
    session, _, claude = session_for(reviewers=reviewers, **every, **options)
    return session, claude


async def review(session: WorkspaceHost, *roles: str) -> None:
    """Queue a review in each of ``roles``."""
    for role in roles:
        await send(session, AddTask(task=TaskSpec(prompt="Review it.", role=role)))


def last_pause(events: list[Event]) -> SessionStateChanged:
    """The last state change among ``events`` saying a runtime is paused."""
    state = [e for e in events if paused(e)][-1]
    assert isinstance(state, SessionStateChanged)
    return state


async def test_a_review_hitting_claudes_limit_pauses_claude_and_runs_again_after() -> None:
    waits = Waits()
    session, claude = reviewing(waits, errs, claims_done, claims_done)
    claude.turn_errors.append(LIMIT)
    await review(session, REVIEWER, REVIEWER)
    events = await until(session, paused)
    await events_until(session, idle("review"))
    assert (roles_of(claude), statuses_of(session)) == ([REVIEWER], ["pending", "pending"])
    state = last_pause(events)

    waits.release.set()
    resumed = await events_until(session, idle("review"))
    await session.close()

    assert (state.paused_for, state.paused_runtime) == (PAUSED_FOR_USAGE_LIMIT, CLAUDE)
    assert tasks_resuming_note(CLAUDE) in notes(resumed)
    assert statuses_of(session) == ["completed", "completed"]
    assert roles_of(claude) == [REVIEWER] * 3


async def test_a_review_finding_claudes_api_down_pauses_claude_for_the_backoff() -> None:
    waits = Waits()
    session, claude = reviewing(waits, errs, claims_done)
    claude.turn_errors.append(OVERLOADED)
    await review(session, REVIEWER)
    events = await until(session, paused)
    waits.release.set()
    await events_until(session, idle("review"))
    await session.close()

    backoff = START + timedelta(minutes=2)
    assert notes(events)[-1] == outage_paused_note(backoff, OVERLOADED, CLAUDE)
    assert last_pause(events).paused_until == backoff
    assert waits.slept == [120.0]
    assert statuses_of(session) == ["completed"]


async def test_a_lane_runs_another_runtimes_task_while_its_review_paused_claude() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), reviewers=(claims_done,))
    roles = {**ROLES, SUPER_REVIEWER: ROLES[SUPER_REVIEWER].model_copy(update={"runtime": FAKE})}
    options: dict[str, Any] = {"host": fake.host, "launchers": {FAKE: fake}, "roles": roles}
    session, claude = reviewing(waits, errs, claims_done, **options)
    claude.turn_errors.append(LIMIT)
    await review(session, REVIEWER, SUPER_REVIEWER)
    await until(session, paused)
    await events_until(session, idle("review"))
    assert (roles_of(fake), statuses_of(session)) == ([SUPER_REVIEWER], ["pending", "completed"])

    waits.release.set()
    await events_until(session, idle("review"))
    await session.close()

    assert statuses_of(session) == ["completed", "completed"]


async def test_cancelling_a_lanes_pause_runs_the_review_it_skipped() -> None:
    waits = Waits()
    session, claude = reviewing(waits, errs, claims_done)
    claude.turn_errors.append(LIMIT)
    await review(session, REVIEWER)
    await until(session, paused)
    await events_until(session, idle("review"))
    await send(session, CancelTurn())
    events = await events_until(session, idle("review"))
    await session.close()

    assert WAIT_CANCELLED_NOTE in notes(events)
    assert statuses_of(session) == ["completed"]


async def test_a_run_for_the_user_replaces_a_lanes_pause_and_runs_the_review_it_skipped() -> None:
    waits = Waits()
    session, claude = reviewing(waits, errs, claims_done)
    claude.turn_errors.append(LIMIT)
    await review(session, REVIEWER)
    await until(session, paused)
    await events_until(session, idle("review"))
    await send(session, RunTasks())
    await events_until(session, idle("review"))
    await session.close()

    assert statuses_of(session) == ["completed"]
    assert roles_of(claude) == [REVIEWER, REVIEWER]


async def test_a_second_runtime_hitting_its_limit_keeps_the_first_pause_and_waits_it_out() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), reviewers=(errs, claims_done, claims_done))
    fake.turn_errors.append(QUOTA)
    fake.failures[QUOTA] = Failure("usage_limit")
    roles = {**ROLES, SUPER_REVIEWER: ROLES[SUPER_REVIEWER].model_copy(update={"runtime": FAKE})}
    options: dict[str, Any] = {"host": fake.host, "launchers": {FAKE: fake}, "roles": roles}
    session, claude = reviewing(waits, errs, claims_done, **options)
    claude.turn_errors.append(LIMIT)
    await review(session, REVIEWER, SUPER_REVIEWER, SUPER_REVIEWER)
    events = await until(session, paused)
    await events_until(session, idle("review"))
    assert statuses_of(session) == ["pending", "pending", "pending"]
    assert roles_of(fake) == [SUPER_REVIEWER]

    waits.release.set()
    await events_until(session, idle("review"))
    await session.close()

    assert last_pause(events).paused_runtime == CLAUDE
    assert waits.slept == [waits.slept[0]]  # Only Claude's pause was waited out.
    assert statuses_of(session) == ["completed"] * 3


def paused_by_a_review(
    waits: Waits, fake: ScriptedLauncher, *scripts: Script, **more: Any
) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session whose drafter runs through ``fake`` and whose coder plays ``scripts`` on
    Claude, its first review hitting Claude's usage limit, timed by ``waits``; ``more`` go to
    :func:`session_for`."""
    every: dict[str, Any] = {"tree": FakeTree(), "review": True, "pause": waits.pause, **more}
    roles = {**ROLES, "drafter": DRAFTER}
    options: dict[str, Any] = {"host": fake.host, "launchers": {FAKE: fake}, "roles": roles}
    reviewers = (errs, claims_done, claims_done, claims_done)
    session, _, claude = session_for(*scripts, reviewers=reviewers, **every, **options)
    claude.turn_errors.append(LIMIT)
    return session, claude


async def wake_in_the_pause(session: WorkspaceHost, task: TaskSpec) -> None:
    """Pause Claude with a review, then queue ``task`` and wake the work lane meanwhile."""
    await review(session, REVIEWER)
    await until(session, paused)
    await events_until(session, idle("review"))
    await send(session, AddTask(task=task))
    await turns_of(session).wake()
    await settled()


async def test_a_wake_in_a_reviews_pause_runs_work_on_another_runtime() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), claims_done)
    session, _ = paused_by_a_review(waits, fake)
    await wake_in_the_pause(session, TaskSpec(prompt="Draft it.", role="drafter"))
    await events_until(session, idle())
    assert turns_of(session).paused is not None
    await session.close()

    assert roles_of(fake) == ["drafter"]


async def test_a_wake_in_a_reviews_pause_with_claude_work_next_runs_it_once_it_ends() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost())
    session, claude = paused_by_a_review(waits, fake, claims_done)
    await wake_in_the_pause(session, TaskSpec(prompt="Code it."))
    assert roles_of(claude) == [REVIEWER]

    waits.release.set()
    events = await events_until(session, idle())
    await session.close()

    assert CODER in roles_of(claude)
    assert tasks_resuming_note(CLAUDE) in notes(events)
    assert RESUMING_NOTE not in notes(events)


async def test_a_wake_in_a_reviews_pause_runs_work_up_to_claude_work_and_the_rest_after() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), claims_done)
    session, claude = paused_by_a_review(waits, fake, claims_done)
    await send(session, AddTask(task=TaskSpec(prompt="Draft it.", role="drafter")))
    await wake_in_the_pause(session, TaskSpec(prompt="Code it."))
    await events_until(session, idle())
    assert roles_of(fake) == ["drafter"]
    assert roles_of(claude) == [REVIEWER]
    state = turns_of(session).paused
    assert state is not None and state.runtime == CLAUDE

    waits.release.set()
    await events_until(session, idle())
    await session.close()

    assert CODER in roles_of(claude)


async def test_a_hold_in_a_reviews_pause_cancels_the_pause_and_keeps_the_work_held() -> None:
    waits = Waits()
    session, claude = paused_by_a_review(waits, ScriptedLauncher(FakeHost()), claims_done)
    await wake_in_the_pause(session, TaskSpec(prompt="Code it."))
    turns_of(session).hold()
    held = await events_until(session, idle("review"))
    cleared = turns_of(session).paused
    await settled()
    assert CODER not in roles_of(claude)

    await send(session, RunTasks())
    await events_until(session, idle())
    await session.close()

    assert WAIT_CANCELLED_NOTE in notes(held)
    assert cleared is None
    assert CODER in roles_of(claude)


async def test_a_lanes_pause_ending_while_halted_starts_the_work_once_the_halt_lifts() -> None:
    waits, store = Waits(), FakeStore()
    session, claude = paused_by_a_review(
        waits, ScriptedLauncher(FakeHost()), claims_done, store=store
    )
    await wake_in_the_pause(session, TaskSpec(prompt="Code it."))
    store.append_failure = "disk full"
    await publish(session, Notice(message="n"))
    await quiet(payloads(session))
    waits.release.set()
    halted = await quiet(payloads(session))
    assert CODER not in roles_of(claude)

    store.append_failure = None
    await send(session, ResumeStorage())
    events = await events_until(session, idle())
    await session.close()

    assert tasks_resuming_note(CLAUDE) not in notes(halted)
    assert notes(events).count(tasks_resuming_note(CLAUDE)) == 1
    assert RESUMING_NOTE not in notes(events)
    assert CODER in roles_of(claude)


def paused_on(runtime: str) -> Stop:
    """Stops when a state change says ``runtime`` is paused."""
    return Stop(
        f"paused_on({runtime!r})",
        lambda e: paused(e) and isinstance(e, SessionStateChanged) and e.paused_runtime == runtime,
    )


async def test_the_work_lanes_own_limit_replaces_a_reviews_pause_and_restarts_the_work() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost(), errs, claims_done)
    fake.turn_errors.append(QUOTA)
    fake.failures[QUOTA] = Failure("usage_limit")
    session, _ = paused_by_a_review(waits, fake)
    await review(session, REVIEWER)
    await until(session, paused)
    await events_until(session, idle("review"))
    await send(session, AddTask(task=TaskSpec(prompt="Draft it.", role="drafter")))
    await turns_of(session).wake()
    replaced = await until(session, paused_on(FAKE))

    waits.release.set()
    resumed = await events_until(session, lambda e: RESUMING_NOTE in notes([e]))
    resumed += await events_until(session, idle())
    resumed += await events_until(session, idle("review"))
    await session.close()

    assert notes(replaced)[-1] == paused_note(START + timedelta(minutes=15), FAKE)
    assert tasks_resuming_note(CLAUDE) not in notes(replaced + resumed)
    assert roles_of(fake) == ["drafter", "drafter"]


async def review_through(session: WorkspaceHost, claude: ScriptedLauncher, *errors: str) -> None:
    """Queue a review whose agents find ``errors`` in turn on Claude, and wait until one ends
    its run without any."""
    claude.turn_errors.extend(errors)
    await review(session, REVIEWER)
    await events_until(session, lambda e: not claude.turn_errors and idle("review")(e))


async def test_a_lanes_outage_backoff_grows_and_starts_over_once_claude_answers() -> None:
    waits = Waits()
    waits.release.set()
    session, claude = reviewing(waits, errs, errs, claims_done, errs, errs, errs, claims_done)
    await review_through(session, claude, OVERLOADED, OVERLOADED)
    await review_through(session, claude, OVERLOADED, LIMIT, OVERLOADED)
    await session.close()

    assert statuses_of(session) == ["completed", "completed"]
    assert waits.slept[:3] == [120.0, 300.0, 120.0]  # Claude answered between the two.
    assert waits.slept[4:] == [120.0]  # The usage limit started the backoff over too.


async def test_an_orbit_queued_review_hitting_claudes_limit_suggests_into_its_batch_after() -> None:
    waits = Waits()
    errors: list[str] = []

    async def hits_the_limit(call: Call) -> TurnEndReason:
        errors.append(LIMIT)
        return "error"

    reviewers = (hits_the_limit, suggests("Add tests"), suggests())
    every: dict[str, Any] = {"tree": FakeTree(), "review": True, "pause": waits.pause}
    session, _, claude = session_for(claims_done, claims_done, reviewers=reviewers, **every)
    claude.turn_errors = errors
    await send(session, AddTask(task=TaskSpec(prompt="Code it.")))
    await send(session, RunTasks())
    await until(session, paused)
    await events_until(session, idle("review"))
    assert statuses_of(session) == ["completed", "pending"]

    waits.release.set()
    events = await settle(session)
    await session.close()

    shown = [(t.task.role, t.status, t.batch) for t in board(events)]
    assert shown == [
        (CODER, "completed", None),
        (REVIEWER, "completed", None),
        (CODER, "completed", "batch-1"),
        (REVIEWER, "completed", None),
    ]
    assert board(events)[-1].task.prompt.startswith("Review the 1 commit")
