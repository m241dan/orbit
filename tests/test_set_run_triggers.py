"""Named sets run from triggers in a session: a ``commits`` trigger's set after every second
landing, in a snapshot of the base, shown under its name; a failing reporting set's notice and
nothing else; none under ``--no-verify`` or without a tree; cancelling a set's agent; the
snapshot showing its panel and agent until a task takes the panel over; and a firing while halted
run once resumed."""

import asyncio
from collections.abc import Callable
from typing import Any

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.application.ports import StoreFailed
from orbit.agent.application.set_runs import SetRuns
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import CheckSet
from orbit.protocol import (
    AgentExited,
    AgentStarted,
    CancelAgent,
    Event,
    Notice,
    RunningAgent,
    StepState,
    TasksChanged,
    VerificationChanged,
)
from tests.agent_fakes import (
    ACCEPTED,
    LINT,
    TRIGGERS,
    FakeStore,
    FakeTree,
    Steps,
    agent_exited,
    claims_done,
    events_until,
    halt_of,
    resume_storage,
    run,
    send,
    session_for,
    sets_of,
    settle,
    settled,
    snapshot_of,
    start,
)
from tests.waiting import eventually

MUTATION = VerificationStep("Mutation tests", ("mutmut", "run"), "slow")
SLOW = CheckSet("slow", (MUTATION,))
SNAPSHOT = "/work/review-base"
CHECKER = RunningAgent(agent_id="set-slow", role="checker", task_id="startup")


def with_slow(steps: Any) -> Verification:
    """Verification with the task check Lint and the set slow, their steps played by
    ``steps``."""
    return Verification((LINT,), steps, sets=(SLOW,))


def slow_trigger(**fields: object) -> dict[str, Trigger]:
    """The shipped triggers, the review off, and ``slow``, running the set slow on each landed
    coder commit, with ``fields`` over that."""
    slow = Trigger.model_validate(
        {"on": "task_landed", "checks": "slow", "when": {"role": "coder"}, **fields}
    )
    review = TRIGGERS["review"].model_copy(update={"enabled": False})
    return {**TRIGGERS, "review": review, "slow": slow}


def panels(events: list[Event]) -> list[tuple[str, ...]]:
    """The statuses of the steps of each panel shown under slow's name, in order."""
    return [
        tuple(step.status for step in event.steps)
        for event in events
        if isinstance(event, VerificationChanged) and event.title == "slow"
    ]


def checked(tasks: int) -> Callable[[list[Event]], bool]:
    """What says the session settled once ``tasks`` tasks completed and slow's run ended."""
    exited = agent_exited("set-slow")

    def done(events: list[Event]) -> bool:
        return completed(events, tasks) and any(exited(event) for event in events)

    return done


def completed(events: list[Event], tasks: int) -> bool:
    """Whether the last task list among ``events`` has ``tasks`` tasks, each completed."""
    boards = [event.tasks for event in events if isinstance(event, TasksChanged)]
    return bool(boards) and [state.status for state in boards[-1]] == ["completed"] * tasks


def started_checkers(events: list[Event]) -> list[AgentStarted]:
    return [e for e in events if isinstance(e, AgentStarted) and e.role == "checker"]


def notices(events: list[Event]) -> list[str]:
    return [event.message for event in events if isinstance(event, Notice)]


async def test_a_commits_trigger_runs_its_set_in_a_snapshot_after_every_second_landing() -> None:
    steps = Steps(*[True] * 6)
    tree = FakeTree()
    session, _, _ = session_for(
        claims_done,
        claims_done,
        claims_done,
        verification=with_slow(steps),
        tree=tree,
        review=True,
        triggers=slow_trigger(on="commits", every=2, when={}),
    )
    await start(session, "One.", "Two.", "Three.")
    events = await asyncio.wait_for(settle(session, until=checked(3)), 10)
    await session.close()

    assert started_checkers(events) == [
        AgentStarted(agent_id="set-slow", role="checker", task_id="startup")
    ]
    assert tree.snapshots == ["base"]
    assert [root for name, root in zip(steps.runs, steps.roots, strict=True) if name != "Lint"] == [
        SNAPSHOT
    ]
    assert panels(events) == [("pending",), ("running",), ("passed",)]
    assert notices(events) == []


async def landed_with_slow(steps: Steps) -> tuple[list[Event], WorkspaceHost]:
    """The events of a session whose one task lands, firing slow, played by ``steps``, until
    slow's run ends, and the session."""
    session, _, _ = session_for(
        claims_done, verification=with_slow(steps), tree=FakeTree(), triggers=slow_trigger()
    )
    await start(session)
    events = await asyncio.wait_for(settle(session, until=checked(1)), 10)
    return events, session


async def test_a_failing_reporting_set_adds_its_notice_and_nothing_else() -> None:
    events, session = await landed_with_slow(Steps(True, False))
    sets = sets_of(session)
    await session.close()

    assert notices(events) == ["slow reported: Mutation tests failed (see the verification panel)"]
    assert panels(events)[-1] == ("failed",)
    boards = [event.tasks for event in events if isinstance(event, TasksChanged)]
    assert [state.origin for state in boards[-1]] == ["manual"]
    assert sets is not None and set(sets.failures) == {"slow"}


async def test_a_passing_set_adds_no_notice() -> None:
    events, session = await landed_with_slow(Steps(True, True))
    await session.close()
    assert panels(events)[-1] == ("passed",)
    assert notices(events) == []


async def test_without_verification_no_set_runs() -> None:
    session, _, _ = session_for(claims_done, tree=FakeTree(), triggers=slow_trigger())
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await settled()
    await session.close()

    assert sets_of(session) is None
    assert started_checkers(events) == []


async def test_without_a_tree_no_set_runs_as_its_trigger_fires() -> None:
    steps = Steps(True, True)
    triggers = slow_trigger(on="task_finished", when={})
    session, _, _ = session_for(claims_done, verification=with_slow(steps), triggers=triggers)
    await start(session)
    events = await asyncio.wait_for(settle(session), 10)
    await settled()
    await session.close()

    assert sets_of(session) is None
    assert started_checkers(events) == []
    assert steps.runs == ["Lint"]


def gated_snapshot() -> tuple[asyncio.Event, Any]:
    """A gate, and steps that pass, those in a snapshot once the gate is set."""
    gate = asyncio.Event()

    async def runner(step: VerificationStep, root: str) -> tuple[bool, str]:
        if root == SNAPSHOT:
            await gate.wait()
        return True, "ok"

    return gate, runner


def running_sets(session: WorkspaceHost) -> SetRuns:
    """The session's runs of the named sets, once slow's is running."""
    sets = sets_of(session)
    assert sets is not None
    sets.run("slow")
    return sets


async def test_cancelling_a_sets_agent_stops_its_run() -> None:
    _, runner = gated_snapshot()
    session, _, _ = session_for(verification=with_slow(runner), tree=FakeTree())
    sets = running_sets(session)
    await eventually(lambda: sets.agents)
    await send(session, CancelAgent(agent_id="set-slow"))
    events = await events_until(session, agent_exited("set-slow"))
    await session.close()

    assert ACCEPTED in events
    assert events[-2:] == [
        Notice(message="slow cancelled."),
        AgentExited(agent_id="set-slow", reason="killed"),
    ]
    assert panels(events)[-1] == ("cancelled",)


async def test_the_snapshot_shows_a_sets_run_until_a_task_takes_the_panel_over() -> None:
    gate, runner = gated_snapshot()
    session, _, _ = session_for(claims_done, verification=with_slow(runner), tree=FakeTree())
    sets = running_sets(session)
    await eventually(lambda: sets.agents)
    shown = await snapshot_of(session)
    assert (shown.steps, shown.steps_title) == (
        (StepState(name="Mutation tests", status="running"),),
        "slow",
    )
    assert shown.agents == (CHECKER,)

    await run(session)
    after = await snapshot_of(session)
    assert (after.steps_title, after.agents) == ("", (CHECKER,))
    gate.set()
    await events_until(session, agent_exited("set-slow"))
    ended = await snapshot_of(session)
    await session.close()
    assert (ended.steps_title, ended.agents) == ("slow", ())


async def test_a_set_fired_while_halted_runs_once_resumed() -> None:
    steps = Steps(True)
    session, _, _ = session_for(verification=with_slow(steps), tree=FakeTree(), store=FakeStore())
    halt_of(session).fail(FailedWrite.EVENT_LOG, StoreFailed("disk full"))
    await settled()
    running_sets(session)
    await settled()
    assert steps.runs == []

    await resume_storage(session)
    await events_until(session, agent_exited("set-slow"))
    await session.close()
    assert steps.runs == ["Mutation tests"]
