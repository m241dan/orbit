"""The rule of which tasks can be edited, which the server and its frontends share: not while a
task runs, and never a review orbit queued itself."""

import pytest

from orbit.protocol import (
    BUSY,
    INVALID,
    MANUAL_ORIGIN,
    PLANNER_ORIGIN,
    REVIEW_ORIGIN,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_REVIEW_ORIGIN,
    SUPER_REVIEWER,
    EditRefusal,
    TaskSpec,
    TaskState,
    TaskStatus,
    edit_refusal,
)

RUNNING = EditRefusal(code=BUSY, message="Task '3' can't be edited while it runs.")
REVIEW = EditRefusal(code=INVALID, message="Reviews can't be edited; remove it instead.")


def test_the_review_roles_are_the_reviewer_and_the_super_reviewer() -> None:
    assert {REVIEWER, SUPER_REVIEWER} == REVIEW_ROLES


@pytest.mark.parametrize("status", ["running", "verifying"])
def test_a_task_cant_be_edited_while_it_runs(status: TaskStatus) -> None:
    state = TaskState(id="3", task=TaskSpec(prompt="go"), status=status)
    assert edit_refusal(state) == RUNNING


def test_a_running_review_is_refused_as_running() -> None:
    task = TaskSpec(prompt="check", role=REVIEWER)
    state = TaskState(id="3", task=task, status="running", origin=REVIEW_ORIGIN)
    assert edit_refusal(state) == RUNNING


@pytest.mark.parametrize("role", sorted(REVIEW_ROLES))
@pytest.mark.parametrize("origin", [REVIEW_ORIGIN, SUPER_REVIEW_ORIGIN])
def test_a_review_orbit_queued_cant_be_edited(role: str, origin: str) -> None:
    for status in ("pending", "completed", "error"):
        state = TaskState(id="1", task=TaskSpec(prompt="check", role=role), status=status)
        assert edit_refusal(state.model_copy(update={"origin": origin})) == REVIEW


@pytest.mark.parametrize("role", sorted(REVIEW_ROLES))
@pytest.mark.parametrize("origin", [MANUAL_ORIGIN, PLANNER_ORIGIN])
def test_a_review_the_user_or_the_planner_queued_can_be_edited(role: str, origin: str) -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="check", role=role), origin=origin)
    assert edit_refusal(state) is None


@pytest.mark.parametrize("origin", [REVIEW_ORIGIN, SUPER_REVIEW_ORIGIN])
def test_a_task_a_review_suggested_can_be_edited(origin: str) -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="tidy"), origin=origin, batch="b1", round=1)
    assert edit_refusal(state) is None


@pytest.mark.parametrize("status", ["pending", "draft", "completed", "cancelled", "gave_up"])
def test_a_settled_or_waiting_coder_task_can_be_edited(status: TaskStatus) -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status=status)
    assert edit_refusal(state) is None
