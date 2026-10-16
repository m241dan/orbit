from datetime import UTC, datetime, timedelta

import pytest

from orbit.agent.domain.usage import (
    MAX_RETRY_SECONDS,
    MIN_REFRESH_SECONDS,
    REFRESH_SECONDS,
    UsageRateLimited,
    UsageReport,
    UsageUnavailable,
    next_delay,
    parse_usage,
    refresh_due,
    usage_event,
)
from orbit.protocol import UsageChanged, UsageLimit

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LIMITS = (UsageLimit(label="Session", percent=52.0),)


def test_parses_the_session_and_week_windows_in_order() -> None:
    payload = {
        "seven_day": {"utilization": 31.5, "resets_at": "2026-10-20T09:00:00Z"},
        "five_hour": {"utilization": 52, "resets_at": "2026-10-16T15:40:00+00:00"},
        "seven_day_opus": {"utilization": 10.0, "resets_at": None},
        "seven_day_sonnet": None,
    }
    assert parse_usage(payload) == (
        UsageLimit(
            label="Session", percent=52.0, resets_at=datetime(2026, 10, 16, 15, 40, tzinfo=UTC)
        ),
        UsageLimit(label="Week", percent=31.5, resets_at=datetime(2026, 10, 20, 9, tzinfo=UTC)),
    )


def test_skips_missing_null_and_unmeasured_windows() -> None:
    assert parse_usage({}) == ()
    assert parse_usage({"five_hour": None, "seven_day": {"utilization": None}}) == ()


def test_a_missing_or_null_reset_time_is_none() -> None:
    payload = {
        "five_hour": {"utilization": 1.0},
        "seven_day": {"utilization": 2.0, "resets_at": None},
    }
    assert parse_usage(payload) == (
        UsageLimit(label="Session", percent=1.0),
        UsageLimit(label="Week", percent=2.0),
    )


def test_a_reset_time_without_a_zone_is_utc() -> None:
    payload = {"five_hour": {"utilization": 1.0, "resets_at": "2026-10-16T15:40:00"}}
    assert parse_usage(payload)[0].resets_at == datetime(2026, 10, 16, 15, 40, tzinfo=UTC)


def test_clamps_utilization_into_range() -> None:
    payload = {"five_hour": {"utilization": -5}, "seven_day": {"utilization": 120.5}}
    assert [limit.percent for limit in parse_usage(payload)] == [0.0, 100.0]


def test_clamps_a_huge_utilization_to_full() -> None:
    assert parse_usage({"five_hour": {"utilization": 10**400}}) == (
        UsageLimit(label="Session", percent=100.0, resets_at=None),
    )


def test_clamps_a_hugely_negative_utilization_to_empty() -> None:
    assert parse_usage({"five_hour": {"utilization": -(10**400)}}) == (
        UsageLimit(label="Session", percent=0.0, resets_at=None),
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "usage",
        {"five_hour": [52.0]},
        {"five_hour": {"utilization": "52"}},
        {"five_hour": {"utilization": True}},
        {"five_hour": {"utilization": float("nan")}},
        {"five_hour": {"utilization": float("inf")}},
        {"seven_day": {"utilization": 1.0, "resets_at": "soon"}},
        {"seven_day": {"utilization": 1.0, "resets_at": 1760000000}},
    ],
)
def test_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_usage(payload)


def test_reports_and_unavailable_outcomes_wait_the_refresh_period() -> None:
    assert next_delay(UsageReport(LIMITS)) == REFRESH_SECONDS
    assert next_delay(UsageUnavailable("no credentials")) == REFRESH_SECONDS


@pytest.mark.parametrize(
    ("retry_after", "delay"),
    [
        (None, REFRESH_SECONDS),
        (0.0, REFRESH_SECONDS),
        (-3.0, REFRESH_SECONDS),
        (10.0, MIN_REFRESH_SECONDS),
        (900.0, 900.0),
        (MAX_RETRY_SECONDS + 1.0, MAX_RETRY_SECONDS),
        (1e300, MAX_RETRY_SECONDS),
        (float("inf"), MAX_RETRY_SECONDS),
        (float("nan"), REFRESH_SECONDS),
    ],
)
def test_a_rate_limit_waits_as_asked_within_the_minimum_and_maximum(
    retry_after: float | None, delay: float
) -> None:
    assert next_delay(UsageRateLimited(retry_after)) == delay


@pytest.mark.parametrize("outcome", [UsageReport(LIMITS), UsageUnavailable("offline")])
def test_refresh_is_due_after_the_minimum_period(outcome: UsageReport | UsageUnavailable) -> None:
    assert not refresh_due(MIN_REFRESH_SECONDS - 1, outcome)
    assert refresh_due(MIN_REFRESH_SECONDS, outcome)


@pytest.mark.parametrize("retry_after", [600.0, None])
def test_refresh_after_a_rate_limit_is_due_once_its_delay_has_passed(
    retry_after: float | None,
) -> None:
    outcome = UsageRateLimited(retry_after)
    assert not refresh_due(MIN_REFRESH_SECONDS, outcome)
    assert not refresh_due(next_delay(outcome) - 1, outcome)
    assert refresh_due(next_delay(outcome), outcome)


def test_a_report_is_ok_with_its_limits() -> None:
    assert usage_event(UsageReport(LIMITS), (), NOW) == UsageChanged(limits=LIMITS)


def test_a_rate_limit_keeps_the_last_limits_and_says_when_it_retries() -> None:
    event = usage_event(UsageRateLimited(120.0), LIMITS, NOW)
    assert event == UsageChanged(
        limits=LIMITS, status="rate_limited", retry_at=NOW + timedelta(seconds=120)
    )


def test_a_huge_rate_limit_retries_after_the_maximum() -> None:
    event = usage_event(UsageRateLimited(1e12), LIMITS, NOW)
    assert event == UsageChanged(
        limits=LIMITS, status="rate_limited", retry_at=NOW + timedelta(seconds=MAX_RETRY_SECONDS)
    )


def test_an_enormous_rate_limit_retries_after_the_maximum() -> None:
    event = usage_event(UsageRateLimited(1e300), (), NOW)
    assert event.retry_at == NOW + timedelta(seconds=MAX_RETRY_SECONDS)


def test_unavailable_keeps_the_last_limits_and_says_why() -> None:
    event = usage_event(UsageUnavailable("not logged in"), LIMITS, NOW)
    assert event == UsageChanged(limits=LIMITS, status="unavailable", detail="not logged in")
