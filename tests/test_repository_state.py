"""What orbit keeps per repository: the super review count, the sampling rotation, the holds on
merges and the schedules' last-fired times, as JSON and read back."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from orbit.agent.domain.merge_holds import FindingHold, Hold
from orbit.agent.domain.repository_state import (
    fired_json,
    merge_holds_json,
    parse_fired,
    parse_merge_holds,
    parse_review_count,
    parse_sampled,
    sampled_json,
)
from orbit.agent.domain.reviews import SuperReviewCount
from tests.session_builders import review_count_json


def test_merge_holds_round_trip_through_json() -> None:
    holds = (
        Hold("stress", "1a2b3c4", "Stress tests", "2 failed", 1),
        FindingHold("bugs-reviewer", "c1", "Fix the crash", "9", "batch-2", 2),
        Hold("soak", "c", "S", ""),
        FindingHold("security-reviewer", "c2", "Escape it", "12", None),
    )
    assert parse_merge_holds(merge_holds_json(holds)) == holds


def test_merge_holds_saved_before_findings_held_merges_read_as_set_holds() -> None:
    saved = '[{"name": "stress", "commit": "c1", "step": "Stress tests", "output": "", "fixes": 1}]'
    assert parse_merge_holds(saved) == (Hold("stress", "c1", "Stress tests", "", 1),)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{}",
        '[{"name": "stress"}]',
        '[{"fixes": -1}]',
        '[{"kind": "finding", "lens": "bugs-reviewer"}]',
    ],
)
def test_unreadable_merge_holds_are_none(text: str) -> None:
    assert parse_merge_holds(text) == ()


def test_the_review_count_round_trips_through_json() -> None:
    assert parse_review_count(review_count_json(7)) == SuperReviewCount(7)


def test_the_review_count_and_last_super_commit_round_trip_through_json() -> None:
    assert parse_review_count(review_count_json(7, "abc")) == SuperReviewCount(7, "abc")


def test_the_review_count_and_its_sample_record_round_trip_through_json() -> None:
    text = '{"since_super": 1, "super_reviews": 3, "sampled": {"a.py": 1, "b.py": 3}}'
    assert parse_review_count(text) == SuperReviewCount(1, None, 3, {"a.py": 1, "b.py": 3})


def test_a_review_count_saved_before_the_sample_was_kept_has_no_super_reviews_or_record() -> None:
    assert parse_review_count(review_count_json(7, "abc")) == SuperReviewCount(7, "abc", 0, {})


def test_an_unreadable_review_count_is_zero() -> None:
    texts = (
        "",
        "{oops",
        "[]",
        '{"since_super": -1}',
        '{"since_super": "many"}',
        '{"since_super": 1, "last_super_commit": 5}',
        '{"since_super": 1, "super_reviews": -1}',
        '{"since_super": 1, "sampled": {"a.py": "often"}}',
        '{"since_super": 1, "sampled": {"a.py": -1}}',
    )
    assert [parse_review_count(text) for text in texts] == [SuperReviewCount()] * 9


def test_the_sample_record_round_trips_through_json() -> None:
    assert parse_sampled(sampled_json({"a.py": 1, "b.py": 3})) == {"a.py": 1, "b.py": 3}


def test_an_unreadable_sample_record_is_empty() -> None:
    texts = ("", "{oops", "[]", '{"a.py": "often"}', '{"a.py": -1}')
    assert [parse_sampled(text) for text in texts] == [{}] * 5


def test_the_last_fired_times_round_trip_through_json() -> None:
    fired = {
        "nightly": datetime(2026, 11, 2, 3, 0, tzinfo=UTC),
        "weekly": datetime(2026, 11, 1, 9, 0, tzinfo=timezone(timedelta(hours=2))),
    }
    assert parse_fired(fired_json(fired)) == fired


def test_unreadable_last_fired_times_are_none() -> None:
    texts = ("", "{oops", "[]", '{"nightly": "soon"}', '{"nightly": "2026-11-02T03:00:00"}')
    assert [parse_fired(text) for text in texts] == [{}] * 5
