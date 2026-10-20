"""Tests for what runs in a session, and the switch refusal it gives."""

import pytest

from orbit.agent.domain.activity import (
    PLANNER_ANSWERING,
    REVIEW_RUNNING,
    TASKS_RUNNING,
    Activity,
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


def test_nothing_in_progress_refuses_nothing_and_is_idle() -> None:
    assert (switch_refusal(Activity()), Activity().running) == (None, False)


def test_the_startup_check_alone_refuses_nothing_and_is_idle() -> None:
    activity = Activity(startup_check=True)

    assert (switch_refusal(activity), activity.running) == (None, False)


def test_everything_in_progress_is_reported_in_order_with_its_task() -> None:
    activity = Activity(
        tasks=True, review=True, planner=True, startup_check=True, task_id="t1", reviewing="t2"
    )

    assert reported(activity) == (
        Reported(kind="task_run", task_id="t1"),
        Reported(kind="review", task_id="t2"),
        Reported(kind="planner"),
        Reported(kind="startup_check"),
    )


def test_only_what_is_in_progress_is_reported() -> None:
    activity = Activity(planner=True, task_id="t1", reviewing="t2")

    assert reported(activity) == (Reported(kind="planner"),)


def test_nothing_in_progress_reports_nothing() -> None:
    assert reported(Activity()) == ()


def test_a_task_run_or_review_whose_task_is_not_known_yet_is_not_reported() -> None:
    assert reported(Activity(tasks=True, review=True)) == ()
