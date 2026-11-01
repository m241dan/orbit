"""Setting up verification first: while verification is on with no steps, every task waits to
run but the configurator's, which sets the steps up, and running tasks is refused, saying why;
adding, editing and accepting tasks, and the conversations, stay open (decisions 7 and 16)."""

from collections.abc import Iterable

from orbit.agent.domain.board import WAITING
from orbit.agent.domain.triggers import VerificationState
from orbit.protocol import CONFIGURATOR, TaskState

CONFIGURING_MESSAGE = "Set up verification first: the configurator is working on it."
SET_UP_MESSAGE = "Set up verification first: press ctrl+e."
"""Why running tasks is refused while verification is missing: while a configurator's task is
still to run or running, and otherwise."""


def waits_for_setup(verification: VerificationState, role: str) -> bool:
    """Whether a task in ``role`` waits for verification to be set up, as ``verification`` says
    it is: while it's missing, every role's but the configurator's."""
    return verification == "missing" and role != CONFIGURATOR


def waiting_for_setup(verification: VerificationState, tasks: Iterable[TaskState]) -> set[str]:
    """The ids of ``tasks`` that wait for verification to be set up, as ``verification`` says
    it is (see :func:`waits_for_setup`)."""
    return {state.id for state in tasks if waits_for_setup(verification, state.task.role)}


def setup_refusal(tasks: Iterable[TaskState]) -> str:
    """Why running tasks is refused while verification is missing, with ``tasks`` on the board:
    the configurator is working on it while one of its tasks is still to run or running; else,
    ctrl+e starts one."""
    configuring = any(
        state.task.role == CONFIGURATOR and state.status in WAITING for state in tasks
    )
    return CONFIGURING_MESSAGE if configuring else SET_UP_MESSAGE


def waiting_note(trigger: str) -> str:
    """What's said of a task the trigger named ``trigger`` queued while verification is missing,
    which waits until it's set up."""
    return f"{trigger}'s task waits: set up verification first."
