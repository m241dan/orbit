"""The triggers on opening a workspace: ``session_started`` is raised once as the host starts its
background work, never on a switch nor while read-only, its ``verification`` as configured; the
``startup-check`` and ``startup-fix`` triggers run the startup check and draft its fix, each off
when disabled, and a project's own trigger on it queues its task."""

import pytest

from orbit.agent import Verification
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    AgentStarted,
    Event,
    NewSession,
    StartupChecked,
    TasksChanged,
    TaskState,
)
from tests.agent_fakes import (
    OWNER,
    TRIGGERS,
    FakeStore,
    FakeTree,
    Steps,
    agent_exited,
    board_of,
    checked,
    events_until,
    payloads,
    send,
    session_for,
)
from tests.connection_helpers import quiet

HELLO = Trigger(
    on="session_started", role=CODER, prompt="Say hello", when={"verification": "configured"}
)
"""A project's trigger queuing a coder's hello as a workspace opens with steps."""


def switched_off(name: str) -> dict[str, Trigger]:
    """The shipped triggers, ``name`` disabled, as ``[triggers.<name>] enabled = false`` does."""
    return {**TRIGGERS, name: TRIGGERS[name].model_copy(update={"enabled": False})}


def started_checks(events: list[Event]) -> list[Event]:
    """The startup check's starts and results among ``events``."""
    return [e for e in events if isinstance(e, AgentStarted | StartupChecked)]


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str, str]]:
    """Each of ``tasks``: its role, origin, status and prompt."""
    return [(t.task.role, t.origin, t.status, t.task.prompt) for t in tasks]


async def test_with_the_startup_check_trigger_disabled_opening_runs_nothing() -> None:
    steps = Steps(True)
    session, _, _ = session_for(
        verification=checked(steps),
        tree=FakeTree(),
        check_at_startup=True,
        triggers=switched_off("startup-check"),
    )
    events = await quiet(payloads(session))
    await session.close()

    assert started_checks(events) == []
    assert steps.runs == []


async def test_with_the_startup_fix_trigger_disabled_a_failing_step_adds_no_draft() -> None:
    session, _, _ = session_for(
        verification=checked(Steps(False)),
        tree=FakeTree(),
        check_at_startup=True,
        triggers=switched_off("startup-fix"),
    )
    events = await events_until(session, agent_exited("startup-check"))
    await session.close()

    assert StartupChecked(passed=False, step="Lint", output="1 test failed") in events
    assert not [e for e in events if isinstance(e, TasksChanged)]


STATES = ("configured", "missing", "off")
"""What ``session_started`` says of a host's verification."""


def verification_for(state: str) -> Verification | None:
    """A host's verification, of steps that pass, as ``session_started`` says ``state``."""
    if state == "off":
        return None
    return checked(Steps(True, True)) if state == "configured" else Verification((), Steps())


@pytest.mark.parametrize("state", STATES)
async def test_opening_queues_the_task_of_the_project_trigger_taking_its_verification(
    state: str,
) -> None:
    triggers = {
        **switched_off("set-up-verification"),
        **{name: HELLO.model_copy(update={"when": {"verification": name}}) for name in STATES},
    }
    session, _, _ = session_for(
        verification=verification_for(state),
        tree=FakeTree(),
        check_at_startup=True,
        triggers=triggers,
    )
    [changed] = [
        e
        for e in await events_until(session, lambda e: isinstance(e, TasksChanged))
        if isinstance(e, TasksChanged)
    ]
    await session.close()

    assert shown(changed.tasks) == [(CODER, state, "pending", "Say hello")]


async def test_without_verification_opening_runs_no_check() -> None:
    session, _, _ = session_for(tree=FakeTree(), check_at_startup=True)
    events = await quiet(payloads(session))
    await session.close()

    assert started_checks(events) == []


async def test_switching_sessions_runs_neither_the_check_nor_the_project_trigger_again() -> None:
    steps = Steps(True, True)
    session, _, _ = session_for(
        verification=checked(steps),
        tree=FakeTree(),
        check_at_startup=True,
        store=FakeStore(),
        triggers={**TRIGGERS, "hello": HELLO},
    )
    await events_until(session, agent_exited("startup-check"))
    assert shown(board_of(session).tasks) == [(CODER, "hello", "pending", "Say hello")]

    await send(session, NewSession(name="other"))
    events = await quiet(payloads(session))
    await session.close()

    assert started_checks(events) == []
    assert steps.runs == ["Lint", "Unit tests"]
    assert board_of(session).tasks == ()


async def test_a_read_only_session_raises_no_session_started() -> None:
    session, _, _ = session_for(
        verification=checked(Steps(True, True)),
        tree=FakeTree(),
        check_at_startup=True,
        owner=OWNER,
        triggers={**TRIGGERS, "hello": HELLO},
    )
    events = await quiet(payloads(session))
    await session.close()

    assert started_checks(events) == []
    assert board_of(session).tasks == ()
