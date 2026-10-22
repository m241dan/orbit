"""Tests for what runs in a session, and the switch refusal it gives."""

import pytest

from orbit.agent.domain.activity import (
    PLANNER_ANSWERING,
    REVIEW_RUNNING,
    TASKS_RUNNING,
    Activity,
    Running,
    doing,
    first_running,
    reported,
    switch_refusal,
)
from orbit.protocol import Activity as Reported


@pytest.mark.parametrize(
    ("activity", "refusal"),
    [
        (Activity(tasks=True), TASKS_RUNNING),
        (Activity(review=True), REVIEW_RUNNING),
        (Activity(planner=True), PLANNER_ANSWERING),
    ],
)
def test_each_activity_alone_refuses_a_switch_saying_so_and_is_running(
    activity: Activity, refusal: str
) -> None:
    assert (switch_refusal(activity), activity.running) == (refusal, True)


@pytest.mark.parametrize(
    ("activity", "refusal"),
    [
        (Activity(tasks=True, review=True, planner=True), TASKS_RUNNING),
        (Activity(review=True, planner=True), REVIEW_RUNNING),
        (Activity(tasks=True, planner=True), TASKS_RUNNING),
    ],
)
def test_several_at_once_refuse_with_the_first_in_order(activity: Activity, refusal: str) -> None:
    assert switch_refusal(activity) == refusal


@pytest.mark.parametrize(
    ("activity", "kind"),
    [
        (Activity(tasks=True, review=True, planner=True), Running.TASKS),
        (Activity(review=True, planner=True), Running.REVIEW),
        (Activity(planner=True), Running.PLANNER),
        (Activity(startup_check=True), None),
    ],
)
def test_the_first_running_kind_is_in_order_and_ignores_the_startup_check(
    activity: Activity, kind: Running | None
) -> None:
    assert first_running(activity) is kind


@pytest.mark.parametrize(
    ("activity", "work"),
    [
        (Activity(tasks=True, review=True, planner=True), "tasks are running"),
        (Activity(review=True, planner=True), "a review is running"),
        (Activity(planner=True), "the planner is answering"),
        (Activity(startup_check=True), None),
    ],
)
def test_what_runs_first_is_called_by_its_kind_and_nothing_when_idle(
    activity: Activity, work: str | None
) -> None:
    assert doing(activity) == work


def test_nothing_in_progress_refuses_nothing_and_is_idle() -> None:
    assert (switch_refusal(Activity()), Activity().running) == (None, False)


def test_the_startup_check_alone_refuses_nothing_and_is_idle() -> None:
    activity = Activity(startup_check=True)

    assert (switch_refusal(activity), activity.running) == (None, False)


def test_everything_in_progress_is_reported_in_order_with_its_task() -> None:
    activity = Activity(
        tasks=True,
        review=True,
        planner=True,
        startup_check=True,
        task_id="t1",
        reviewing=("t2",),
        waiting_for_coder=2,
        coders_busy=3,
    )

    assert reported(activity) == (
        Reported(kind="task_run", task_id="t1"),
        Reported(kind="waiting_for_coder", position=2, busy=3),
        Reported(kind="review", task_id="t2"),
        Reported(kind="planner"),
        Reported(kind="startup_check"),
    )


def test_only_what_is_in_progress_is_reported() -> None:
    activity = Activity(planner=True, task_id="t1", reviewing=("t2",))

    assert reported(activity) == (Reported(kind="planner"),)


def test_nothing_in_progress_reports_nothing() -> None:
    assert reported(Activity()) == ()


def test_a_task_run_or_review_whose_task_is_not_known_yet_is_not_reported() -> None:
    assert reported(Activity(tasks=True, review=True)) == ()


def test_a_review_of_several_tasks_is_reported_once_per_task_in_order() -> None:
    activity = Activity(review=True, reviewing=("3", "5"))

    assert reported(activity) == (
        Reported(kind="review", task_id="3"),
        Reported(kind="review", task_id="5"),
    )


def test_a_review_of_no_particular_task_is_reported_once_without_one() -> None:
    assert reported(Activity(review=True, reviewing=())) == (Reported(kind="review"),)


def test_a_task_run_waiting_for_a_coder_is_reported_with_its_position() -> None:
    activity = Activity(tasks=True, task_id="t1", waiting_for_coder=1, coders_busy=4)

    assert reported(activity) == (
        Reported(kind="task_run", task_id="t1"),
        Reported(kind="waiting_for_coder", position=1, busy=4),
    )
