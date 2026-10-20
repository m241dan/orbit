"""Tests for what runs in a session, and the switch refusal it gives."""

import pytest

from orbit.agent.domain.activity import (
    PLANNER_ANSWERING,
    REVIEW_RUNNING,
    STARTUP_CHECKING,
    TASKS_RUNNING,
    Activity,
    switch_refusal,
)


@pytest.mark.parametrize(
    ("activity", "refusal"),
    [
        (Activity(tasks=True), TASKS_RUNNING),
        (Activity(review=True), REVIEW_RUNNING),
        (Activity(planner=True), PLANNER_ANSWERING),
        (Activity(startup_check=True), STARTUP_CHECKING),
    ],
)
def test_each_activity_alone_refuses_a_switch_saying_so_and_is_running(
    activity: Activity, refusal: str
) -> None:
    assert (switch_refusal(activity), activity.running) == (refusal, True)


@pytest.mark.parametrize(
    ("activity", "refusal"),
    [
        (Activity(tasks=True, review=True, planner=True, startup_check=True), TASKS_RUNNING),
        (Activity(review=True, planner=True, startup_check=True), REVIEW_RUNNING),
        (Activity(planner=True, startup_check=True), PLANNER_ANSWERING),
        (Activity(tasks=True, startup_check=True), TASKS_RUNNING),
    ],
)
def test_several_at_once_refuse_with_the_first_in_order(activity: Activity, refusal: str) -> None:
    assert switch_refusal(activity) == refusal


def test_nothing_in_progress_refuses_nothing_and_is_idle() -> None:
    assert (switch_refusal(Activity()), Activity().running) == (None, False)
