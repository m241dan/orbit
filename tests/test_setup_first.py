"""Setting up verification first: while it's missing, every task waits but the configurator's,
and running tasks is refused, saying why."""

import pytest

from orbit.agent.domain.setup_first import (
    CONFIGURING_MESSAGE,
    SET_UP_MESSAGE,
    setup_refusal,
    waiting_for_setup,
    waiting_note,
    waits_for_setup,
)
from orbit.agent.domain.triggers import VerificationState
from orbit.protocol import CODER, CONFIGURATOR, REVIEWER, TaskSpec, TaskState, TaskStatus


def state(task_id: str, role: str, status: TaskStatus = "pending") -> TaskState:
    """A task ``task_id`` of ``role`` that's ``status``."""
    return TaskState(id=task_id, task=TaskSpec(prompt="Do it.", role=role), status=status)


def test_the_refusals_and_the_note_say_to_set_up_verification_first() -> None:
    assert CONFIGURING_MESSAGE == "Set up verification first: the configurator is working on it."
    assert SET_UP_MESSAGE == "Set up verification first: press ctrl+e."
    assert waiting_note("review") == "review's task waits: set up verification first."


def test_while_verification_is_missing_every_task_waits_but_the_configurators() -> None:
    assert waits_for_setup("missing", CODER)
    assert waits_for_setup("missing", REVIEWER)
    assert not waits_for_setup("missing", CONFIGURATOR)


@pytest.mark.parametrize("verification", ["configured", "off"])
def test_with_verification_configured_or_off_no_task_waits(
    verification: VerificationState,
) -> None:
    assert not waits_for_setup(verification, CODER)


def test_the_tasks_waiting_for_setup_are_all_but_the_configurators() -> None:
    tasks = [state("1", CONFIGURATOR), state("2", CODER), state("3", REVIEWER, "draft")]
    assert waiting_for_setup("missing", tasks) == {"2", "3"}
    assert waiting_for_setup("configured", tasks) == set()


@pytest.mark.parametrize("status", ["pending", "running", "verifying"])
def test_running_is_refused_as_the_configurator_works_while_its_task_waits(
    status: TaskStatus,
) -> None:
    tasks = [state("1", CODER), state("2", CONFIGURATOR, status)]
    assert setup_refusal(tasks) == CONFIGURING_MESSAGE


@pytest.mark.parametrize("status", ["completed", "cancelled", "error", "gave_up"])
def test_running_is_refused_saying_to_press_ctrl_e_once_the_configurator_ended(
    status: TaskStatus,
) -> None:
    tasks = [state("1", CONFIGURATOR, status), state("2", CODER)]
    assert setup_refusal(tasks) == SET_UP_MESSAGE


def test_running_is_refused_saying_to_press_ctrl_e_without_a_configurator() -> None:
    assert setup_refusal([state("1", CODER)]) == SET_UP_MESSAGE
