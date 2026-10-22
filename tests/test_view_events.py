from itertools import combinations
from typing import get_args

import pytest

from orbit.protocol import (
    AgentStarted,
    AssistantMessage,
    Event,
    Notice,
    ReviewStatus,
    Settings,
    SettingsChanged,
    StepState,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui.view.events import (
    ANSWER_EVENTS,
    FEED_EVENTS,
    SESSION_EVENTS,
    STATE_EVENTS,
    coalesce,
    in_group,
)

GROUPS: tuple[tuple[type[Event], ...], ...] = (
    STATE_EVENTS,
    FEED_EVENTS,
    SESSION_EVENTS,
    ANSWER_EVENTS,
)
"""The groupings the app routes events by."""

IGNORED: frozenset[type[Event]] = frozenset()
"""The events the TUI deliberately doesn't render: none."""


def tasks_changed(*task_ids: str) -> TasksChanged:
    return TasksChanged(tasks=tuple(TaskState(id=i, task=TaskSpec(prompt=i)) for i in task_ids))


def steps_changed(*names: str) -> VerificationChanged:
    return VerificationChanged(steps=tuple(StepState(name=name) for name in names))


def test_coalesce_collapses_consecutive_task_lists_to_the_last() -> None:
    last = tasks_changed("1", "2", "3")
    assert coalesce([tasks_changed("1"), tasks_changed("1", "2"), last]) == [last]


@pytest.mark.parametrize(
    "blocker",
    [
        TaskStarted(task_id="1"),
        VerificationStarted(task_id="1", step=0),
        VerificationSkipped(task_id="1", reason="no_changes"),
        VerificationFinished(task_id="1", step=0, passed=True),
        TaskMerged(task_id="1", result="merged"),
    ],
)
def test_coalesce_keeps_a_task_list_an_event_reads(blocker: Event) -> None:
    events: list[Event] = [tasks_changed("1"), blocker, tasks_changed("1", "2")]
    assert coalesce(events) == events


@pytest.mark.parametrize(
    "between",
    [Notice(message="n"), AgentStarted(agent_id="agent-1", role="coder", task_id="1")],
)
def test_coalesce_drops_a_task_list_past_events_that_dont_read_it(between: Event) -> None:
    last = tasks_changed("1", "2")
    assert coalesce([tasks_changed("1"), between, last]) == [between, last]


def test_coalesce_drops_only_the_task_lists_nothing_reads() -> None:
    first, read, last = tasks_changed("1"), tasks_changed("1", "2"), tasks_changed("2")
    merged = TaskMerged(task_id="1", result="merged")
    assert coalesce([first, read, merged, last]) == [read, merged, last]


def test_coalesce_collapses_consecutive_verification_steps_to_the_last() -> None:
    last = steps_changed("Lint", "Test")
    assert coalesce([steps_changed("Lint"), last]) == [last]


def test_coalesce_keeps_verification_steps_a_verification_note_reads() -> None:
    for note in (
        VerificationStarted(task_id="1", step=0),
        VerificationFinished(task_id="1", step=0, passed=True),
    ):
        events: list[Event] = [steps_changed("Lint"), note, steps_changed("Test")]
        assert coalesce(events) == events


def test_coalesce_verification_steps_ignore_a_task_start_between() -> None:
    last = steps_changed("Test")
    started = TaskStarted(task_id="1")
    assert coalesce([steps_changed("Lint"), started, last]) == [started, last]


def test_coalesce_always_collapses_review_status_and_settings() -> None:
    review = ReviewStatus(paused=True, open_batches=1)
    settings = SettingsChanged(settings=Settings(model="opus"))
    events: list[Event] = [
        ReviewStatus(paused=False, open_batches=0),
        SettingsChanged(settings=Settings()),
        TaskStarted(task_id="1"),
        VerificationStarted(task_id="1", step=0),
        review,
        settings,
    ]
    assert coalesce(events) == events[2:]


def test_coalesce_keeps_other_events_in_order() -> None:
    events: list[Event] = [
        AssistantMessage(turn_id="t", text="a"),
        Notice(message="n"),
        AssistantMessage(turn_id="t", text="a"),
        TaskStarted(task_id="1"),
        TaskStarted(task_id="1"),
    ]
    assert coalesce(events) == events


def test_coalesce_passes_empty_and_single_batches_through() -> None:
    assert coalesce([]) == []
    assert coalesce([tasks_changed("1")]) == [tasks_changed("1")]


def test_groupings_are_disjoint() -> None:
    for first, second in combinations(GROUPS, 2):
        assert not set(first) & set(second)


def test_groupings_cover_every_event_but_those_ignored() -> None:
    (union, *_) = get_args(Event)
    rendered = {kind for group in GROUPS for kind in group}
    assert rendered == set(get_args(union)) - IGNORED


def test_in_group_is_whether_the_event_is_of_the_group() -> None:
    assert in_group(tasks_changed("1"), STATE_EVENTS)
    assert not in_group(tasks_changed("1"), FEED_EVENTS)
