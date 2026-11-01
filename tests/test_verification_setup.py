"""Setting up verification first: a workspace opening with verification on and no steps queues
the configurator's task first and runs it, running tasks is refused until its checks are
accepted, a triggered task waits meanwhile, with a notice, and the planner answers throughout;
with ``--no-verify`` nothing is refused."""

import asyncio

from orbit.agent import NO_STEPS_NOTE, Verification, WorkspaceHost
from orbit.agent.application.ports import ProposalDecision
from orbit.agent.domain.setup_first import CONFIGURING_MESSAGE, SET_UP_MESSAGE
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    NOT_CONFIGURED,
    PLANNER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    AddTask,
    CommandRefused,
    FireTrigger,
    Notice,
    RunTasks,
    SendMessage,
    TaskSpec,
    TurnEndReason,
    TurnFinished,
)
from tests.agent_fakes import (
    TRIGGERS,
    Call,
    Configurator,
    FakeTree,
    Script,
    Steps,
    agent_exited,
    board,
    board_of,
    chats,
    checked,
    claims_done,
    events_until,
    no_steps,
    of_type,
    refused,
    run,
    send,
    session_for,
    settle,
    settled,
    statuses,
    suggests,
    until_idle,
)
from tests.lane_fakes import running
from tests.proposal_fakes import LINT, TESTS, Review, proposes

CODE = TaskSpec(prompt="Add a flag.")

WAITING = Notice(message="super-review's task waits: set up verification first.")
"""The notice of a super review fired by hand while verification is missing."""


def switched_off(name: str) -> dict[str, Trigger]:
    """The shipped triggers, ``name`` disabled, as ``[triggers.<name>] enabled = false`` does."""
    return {**TRIGGERS, name: TRIGGERS[name].model_copy(update={"enabled": False})}


async def refusal_of_run(session: WorkspaceHost) -> CommandRefused:
    """The refusal of ``RunTasks`` sent to ``session`` now."""
    await send(session, RunTasks())
    events = await events_until(session, of_type(CommandRefused))
    refusal = events[-1]
    assert isinstance(refusal, CommandRefused)
    return refusal


def accepting(*scripts: Script, steps: Steps) -> tuple[WorkspaceHost, list[str]]:
    """A session opening with no steps, whose configurator proposes Lint and Tests, which the
    user accepts, verified by ``steps``; ``scripts`` play the agents of the tasks after it.
    Returns it with orbit's replies to the configurator."""
    replies: list[str] = []
    session, _, _ = session_for(
        proposes({"steps": [LINT, TESTS]}, replies=replies),
        *scripts,
        verification=Verification((), steps),
        proposal_review=Review(ProposalDecision("accept")),
        check_at_startup=True,
    )
    return session, replies


async def test_opening_without_steps_queues_the_configurators_task_and_runs_it() -> None:
    configurator = Configurator()
    session, _, launcher = session_for(configurator, verification=no_steps(), check_at_startup=True)
    events = await until_idle(session)
    await session.close()

    [task] = board(events)
    assert (task.task.role, task.origin, task.status) == (
        CONFIGURATOR,
        "set-up-verification",
        "unverified",
    )
    assert [launch.agent.role for launch in launcher.launches] == [CONFIGURATOR]
    assert "Nothing is configured yet." in configurator.tasks[0]


async def test_opening_with_steps_queues_no_configurator() -> None:
    session, _, launcher = session_for(
        Configurator(),
        verification=checked(Steps(True, True)),
        tree=FakeTree(),
        check_at_startup=True,
    )
    await events_until(session, agent_exited("startup-check"))
    await settled()
    await session.close()

    assert board_of(session).tasks == ()
    assert launcher.launches == []


async def test_a_run_is_refused_as_the_configurator_works_then_saying_to_press_ctrl_e() -> None:
    gate, configurator = asyncio.Event(), Configurator()

    async def waits(call: Call) -> TurnEndReason:
        await gate.wait()
        return await configurator(call)

    session, _, launcher = session_for(waits, verification=no_steps(), check_at_startup=True)
    await events_until(session, running)
    await send(session, AddTask(task=CODE))
    working = await refusal_of_run(session)
    gate.set()
    await until_idle(session)
    ended = await refusal_of_run(session)
    await session.close()

    assert working == refused(NOT_CONFIGURED, CONFIGURING_MESSAGE)
    assert ended == refused(NOT_CONFIGURED, SET_UP_MESSAGE)
    assert [launch.agent.role for launch in launcher.launches] == [CONFIGURATOR]
    assert [t.status for t in board_of(session).tasks] == ["unverified", "pending"]


async def test_once_the_configurators_checks_are_accepted_a_run_runs_tasks() -> None:
    steps = Steps(True, True, True, True)
    session, replies = accepting(claims_done, steps=steps)
    await until_idle(session)
    events = await run(session, CODE)
    await session.close()

    assert len(replies) == 1
    assert statuses(events) == ["completed", "completed"]
    assert steps.runs == ["Lint", "Tests", "Lint", "Tests"]


async def test_a_super_review_fired_while_refused_waits_with_a_notice_until_steps_exist() -> None:
    replies: list[str] = []
    session, _, _ = session_for(
        proposes({"steps": [LINT]}, replies=replies),
        reviewers=(suggests(),),
        tree=FakeTree(),
        review=True,
        verification=Verification((), Steps(True)),
        proposal_review=Review(ProposalDecision("accept")),
    )
    await send(session, FireTrigger(trigger=SUPER_REVIEW))
    waiting = await events_until(session, lambda event: event == WAITING)
    await settled()
    held = [(t.task.role, t.status) for t in board_of(session).tasks]

    await send(session, FireTrigger(trigger=CONFIGURE_CHECKS))
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()

    assert Notice(message=NO_STEPS_NOTE) in waiting
    assert held == [(SUPER_REVIEWER, "pending")]
    shown = [(t.task.role, t.status) for t in board(events)]
    assert shown == [(CONFIGURATOR, "completed"), (SUPER_REVIEWER, "completed")]


async def test_with_no_verify_opening_starts_no_configurator_and_runs_are_not_refused() -> None:
    session, _, launcher = session_for(claims_done, check_at_startup=True)
    events = await run(session, CODE)
    await session.close()

    assert statuses(events) == ["completed"]
    assert [launch.agent.role for launch in launcher.launches] == [CODER]


async def test_with_set_up_verification_disabled_opening_starts_none_and_runs_stay_refused() -> (
    None
):
    session, _, launcher = session_for(
        Configurator(),
        verification=no_steps(),
        check_at_startup=True,
        triggers=switched_off("set-up-verification"),
    )
    await send(session, AddTask(task=CODE))
    refusal = await refusal_of_run(session)
    await settled()
    await session.close()

    assert refusal == refused(NOT_CONFIGURED, SET_UP_MESSAGE)
    assert [t.task.role for t in board_of(session).tasks] == [CODER]
    assert launcher.launches == []


async def test_the_planner_answers_while_the_configurator_works() -> None:
    gate = asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await gate.wait()
        return "completed"

    session, _, launcher = session_for(waits, verification=no_steps(), check_at_startup=True)
    launcher.planner_turns = [chats]
    await events_until(session, running)
    await send(session, SendMessage(agent=PLANNER, text="hello"))
    events = await events_until(session, of_type(TurnFinished))
    gate.set()
    await until_idle(session)
    await session.close()

    assert any(isinstance(e, TurnFinished) and e.reason == "completed" for e in events)
