"""One outage backoff per runtime, whichever lane's agent finds its API not answering: outages on
a runtime in the work lane and in a lane that starts by itself count in a row together, an agent
on that runtime finishing its run in either lane or hitting its usage limit starts the backoff over
for both, and each runtime's backoff is its own."""

import asyncio
from datetime import timedelta
from typing import Any

from orbit.agent import WorkspaceHost
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.domain.roles import Role
from orbit.agent.domain.runtimes import CLAUDE
from orbit.agent.domain.usage_limits import outage_paused_note
from orbit.protocol import (
    AddTask,
    CancelTurn,
    Event,
    Notice,
    RemoveTask,
    RunTasks,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    START,
    Call,
    FakeHost,
    Script,
    ScriptedLauncher,
    claims_done,
    events_until,
    idle,
    run,
    send,
    session_for,
    settled,
)
from tests.lane_fakes import WITH_SECURITY, audit

FAKE = "fake"
"""A runtime other than Claude, played by a launcher of its own."""

OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'

LIMIT = "You've hit your session limit · resets 6pm (UTC)"

DRAFTER = Role(instructions="Draft it.", verify=False, runtime=FAKE)
"""A role in the work lane, on the fake runtime."""

SCANNER = Role(
    instructions="Scan the code.", verify=False, lane="security", workspace="snapshot", runtime=FAKE
)
"""A role in the ``security`` lane, on the fake runtime."""


class Waits:
    """A pause whose clock stands at :data:`START` and whose sleeps, noted in minutes, last until
    cancelled."""

    def __init__(self) -> None:
        self.minutes: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.minutes.append(seconds / 60)
        await asyncio.Event().wait()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: START, sleep=self.sleep)


def fails(launcher: ScriptedLauncher, message: str) -> Script:
    """A script whose turn ends in error with ``message``, as the CLI reports it."""

    async def script(call: Call) -> TurnEndReason:
        launcher.turn_errors.append(message)
        return "error"

    return script


def auditing(waits: Waits, **options: Any) -> tuple[WorkspaceHost, ScriptedLauncher]:
    """A session with a ``security`` lane that starts by itself beside the work lane, every role
    on Claude unless ``options`` give other ``roles``, timed by ``waits``; ``options`` go to
    :func:`session_for`."""
    session, _, claude = session_for(pause=waits.pause, **{"roles": WITH_SECURITY, **options})
    return session, claude


async def audited(session: WorkspaceHost, task: TaskSpec | None = None) -> list[Event]:
    """Queue ``task``, or an audit if none is given, which the ``security`` lane starts by
    itself, and its events until the lane is idle, with any pause it started waiting."""
    await send(session, AddTask(task=task or audit()))
    events = await events_until(session, idle("security"))
    await settled()  # A pause's wait gets as far as its sleep.
    return events


async def ran(session: WorkspaceHost, *tasks: TaskSpec) -> list[Event]:
    """Run ``tasks``, or those waiting if none are given, and their events until the work lane
    is idle, with any pause it started waiting."""
    if tasks:
        events = await run(session, *tasks)
    else:
        await send(session, RunTasks())
        events = await events_until(session, idle())
    await settled()  # A pause's wait gets as far as its sleep.
    return events


def notes(events: list[Event]) -> list[str]:
    return [e.message for e in events if isinstance(e, Notice)]


def outage_note(minutes: int, runtime: str = CLAUDE) -> str:
    """What's said of an outage on ``runtime`` that pauses it for ``minutes``."""
    return outage_paused_note(START + timedelta(minutes=minutes), OVERLOADED, runtime)


async def test_the_work_lanes_outage_after_another_lanes_on_its_runtime_waits_longer() -> None:
    waits = Waits()
    session, claude = auditing(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, OVERLOADED)])
    await audited(session)
    await send(session, RemoveTask(task_id="1"))
    events = await ran(session, TaskSpec(prompt="Code it."))
    await session.close()

    assert waits.minutes == [2, 5]
    assert notes(events)[-1] == outage_note(5)


async def test_another_lanes_outage_after_the_work_lanes_on_its_runtime_waits_longer() -> None:
    waits = Waits()
    session, claude = auditing(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, OVERLOADED)])
    await ran(session, TaskSpec(prompt="Code it."))
    await send(session, CancelTurn())
    events = await audited(session)
    await session.close()

    assert waits.minutes == [2, 5]
    assert notes(events)[-1] == outage_note(5)


async def test_an_agent_finishing_in_another_lane_starts_the_work_lanes_backoff_over() -> None:
    waits = Waits()
    session, claude = auditing(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), claims_done, fails(claude, OVERLOADED)])
    await ran(session, TaskSpec(prompt="Code it."))
    await send(session, CancelTurn())
    await audited(session)
    events = await ran(session)
    await session.close()

    assert waits.minutes == [2, 2]
    assert notes(events)[-1] == outage_note(2)


async def test_an_agent_finishing_in_the_work_lane_starts_another_lanes_backoff_over() -> None:
    waits = Waits()
    session, claude = auditing(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), claims_done, fails(claude, OVERLOADED)])
    await audited(session)
    await send(session, RemoveTask(task_id="1"))
    await ran(session, TaskSpec(prompt="Code it."))
    events = await audited(session)
    await session.close()

    assert waits.minutes == [2, 2]
    assert notes(events)[-1] == outage_note(2)


async def test_another_lanes_usage_limit_starts_the_work_lanes_backoff_over() -> None:
    waits = Waits()
    session, claude = auditing(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, LIMIT)])
    claude.scripts.append(fails(claude, OVERLOADED))
    await ran(session, TaskSpec(prompt="Code it."))
    await send(session, CancelTurn())
    await audited(session)
    await send(session, RemoveTask(task_id="2"))
    events = await ran(session)
    await session.close()

    assert waits.minutes == [2, 15, 2]
    assert notes(events)[-1] == outage_note(2)


async def test_the_work_lanes_usage_limit_starts_another_lanes_backoff_over() -> None:
    waits = Waits()
    session, claude = auditing(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, LIMIT)])
    claude.scripts.append(fails(claude, OVERLOADED))
    await audited(session)
    await send(session, RemoveTask(task_id="1"))
    await ran(session, TaskSpec(prompt="Code it."))
    await send(session, CancelTurn())
    events = await audited(session)
    await session.close()

    assert waits.minutes == [2, 15, 2]
    assert notes(events)[-1] == outage_note(2)


async def test_an_outage_on_another_runtime_leaves_claudes_backoff_going() -> None:
    waits = Waits()
    fake = ScriptedLauncher(FakeHost())
    fake.scripts.append(fails(fake, OVERLOADED))
    roles = {**WITH_SECURITY, "drafter": DRAFTER}
    session, claude = auditing(waits, host=fake.host, launchers={FAKE: fake}, roles=roles)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, OVERLOADED)])
    await ran(session, TaskSpec(prompt="Code it."))
    await send(session, RemoveTask(task_id="1"))
    drafted = await ran(session, TaskSpec(prompt="Draft it.", role="drafter"))
    await send(session, RemoveTask(task_id="2"))
    events = await ran(session, TaskSpec(prompt="Code more."))
    await session.close()

    assert waits.minutes == [2, 2, 5]
    assert notes(drafted)[-1] == outage_note(2, FAKE)
    assert notes(events)[-1] == outage_note(5)


def scanning(waits: Waits) -> tuple[WorkspaceHost, ScriptedLauncher, ScriptedLauncher]:
    """A session as :func:`auditing` gives, timed by ``waits``, with a ``scanner`` on the fake
    runtime in the ``security`` lane, whose agent finds its API not answering, and Claude's and
    the fake runtime's launchers."""
    fake = ScriptedLauncher(FakeHost())
    fake.scripts.append(fails(fake, OVERLOADED))
    roles = {**WITH_SECURITY, "scanner": SCANNER}
    session, claude = auditing(waits, host=fake.host, launchers={FAKE: fake}, roles=roles)
    return session, claude, fake


async def paused_by_a_scan(session: WorkspaceHost) -> None:
    """Have the work lane's run stop at Claude's outage and its wait cancelled, then a scan,
    on the fake runtime, pause that runtime from the ``security`` lane."""
    await ran(session, TaskSpec(prompt="Code it."))
    await send(session, CancelTurn())
    await audited(session, TaskSpec(prompt="Scan it.", role="scanner"))


async def test_a_lanes_usage_limit_during_another_pause_starts_its_runtimes_backoff_over() -> None:
    waits = Waits()
    session, claude, _ = scanning(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, LIMIT)])
    claude.scripts.append(fails(claude, OVERLOADED))
    await paused_by_a_scan(session)
    await audited(session)
    await send(session, RemoveTask(task_id="2"))
    await send(session, RemoveTask(task_id="3"))
    events = await ran(session)
    await session.close()

    assert waits.minutes == [2, 2, 2]
    assert notes(events)[-1] == outage_note(2)


async def test_a_lanes_outage_during_another_pause_pauses_nothing_and_counts_for_nothing() -> None:
    waits = Waits()
    session, claude, _ = scanning(waits)
    claude.scripts.extend([fails(claude, OVERLOADED), fails(claude, OVERLOADED)])
    claude.scripts.append(fails(claude, OVERLOADED))
    await paused_by_a_scan(session)
    audit_events = await audited(session)
    minutes = list(waits.minutes)
    await send(session, RemoveTask(task_id="2"))
    await send(session, RemoveTask(task_id="3"))
    events = await ran(session)
    await session.close()

    assert minutes == [2, 2]
    assert outage_note(2) not in notes(audit_events)
    assert waits.minutes == [2, 2, 5]
    assert notes(events)[-1] == outage_note(5)
