"""The rule of which tasks can be edited, which the server and its frontends share: not while a
task runs, and never a review orbit queued itself, a task it queued as a review, whatever its
role."""

import pytest

from orbit.protocol import (
    BUSY,
    CODER,
    INVALID,
    LENS_REVIEWERS,
    MANUAL_ORIGIN,
    PLANNER_ORIGIN,
    REVIEW_ORIGIN,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_LENS_REVIEWERS,
    SUPER_REVIEW,
    EditRefusal,
    TaskSpec,
    TaskState,
    TaskStatus,
    edit_refusal,
    is_orbit_review,
)

AUDIT = "audit"
"""A project's own trigger, such as a ``commits`` one queuing a reviewer's task."""

RUNNING = EditRefusal(code=BUSY, message="Task '3' can't be edited while it runs.")
REVIEW = EditRefusal(code=INVALID, message="Reviews can't be edited; remove it instead.")


def test_the_review_roles_are_the_lens_reviewers_and_the_reviewer() -> None:
    assert {REVIEWER, *LENS_REVIEWERS, *SUPER_LENS_REVIEWERS} == REVIEW_ROLES


@pytest.mark.parametrize("status", ["running", "verifying"])
def test_a_task_cant_be_edited_while_it_runs(status: TaskStatus) -> None:
    state = TaskState(id="3", task=TaskSpec(prompt="go"), status=status)
    assert edit_refusal(state) == RUNNING


def test_a_running_review_is_refused_as_running() -> None:
    task = TaskSpec(prompt="check", role=REVIEWER)
    state = TaskState(id="3", task=task, status="running", origin=REVIEW_ORIGIN, queued_as="review")
    assert edit_refusal(state) == RUNNING


AUDITOR = "auditor"
"""A role of a project's own, outside the review roles."""


@pytest.mark.parametrize("role", [*sorted(REVIEW_ROLES), CODER, AUDITOR])
@pytest.mark.parametrize("status", ["pending", "draft", "completed", "error"])
def test_a_task_queued_as_a_review_cant_be_edited_whatever_its_role(
    role: str, status: TaskStatus
) -> None:
    task = TaskSpec(prompt="check", role=role)
    state = TaskState(id="1", task=task, status=status, origin=AUDIT, queued_as="review")
    assert edit_refusal(state) == REVIEW


@pytest.mark.parametrize("role", sorted(REVIEW_ROLES))
@pytest.mark.parametrize("origin", [MANUAL_ORIGIN, PLANNER_ORIGIN, REVIEW_ORIGIN, AUDIT])
@pytest.mark.parametrize("status", ["pending", "draft", "completed"])
def test_a_review_roles_task_not_queued_as_a_review_can_be_edited(
    role: str, origin: str, status: TaskStatus
) -> None:
    task = TaskSpec(prompt="check", role=role)
    state = TaskState(id="1", task=task, status=status, origin=origin)
    assert edit_refusal(state) is None


@pytest.mark.parametrize("origin", [REVIEW_ORIGIN, SUPER_REVIEW, AUDIT])
def test_a_task_a_review_suggested_can_be_edited(origin: str) -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="tidy"), origin=origin, batch="b1", round=1)
    assert edit_refusal(state) is None


@pytest.mark.parametrize("status", ["pending", "draft", "completed", "cancelled", "gave_up"])
def test_a_settled_or_waiting_coder_task_can_be_edited(status: TaskStatus) -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="go"), status=status)
    assert edit_refusal(state) is None


@pytest.mark.parametrize("role", [*sorted(REVIEW_ROLES), CODER, AUDITOR])
def test_a_task_queued_as_a_review_is_a_review_orbit_queued(role: str) -> None:
    task = TaskSpec(prompt="check", role=role)
    state = TaskState(id="1", task=task, origin=AUDIT, queued_as="review")
    assert is_orbit_review(state)


@pytest.mark.parametrize("origin", [MANUAL_ORIGIN, PLANNER_ORIGIN, REVIEW_ORIGIN, SUPER_REVIEW])
def test_a_review_roles_task_queued_as_a_task_is_not_one_orbit_queued(origin: str) -> None:
    for role in REVIEW_ROLES:
        state = TaskState(id="1", task=TaskSpec(prompt="check", role=role), origin=origin)
        assert not is_orbit_review(state)


def test_a_task_a_review_suggested_is_not_a_review() -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="tidy"), origin=AUDIT, batch="b1", round=1)
    assert not is_orbit_review(state)


def test_a_task_is_queued_as_a_task_unless_it_says_otherwise() -> None:
    assert TaskState(id="1", task=TaskSpec(prompt="go")).queued_as == "task"
