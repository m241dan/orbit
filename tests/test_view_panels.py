from datetime import UTC, datetime, timedelta, timezone

from orbit.protocol import (
    StepState,
    UsageChanged,
    UsageLimit,
)
from orbit.tui.view import (
    reset_label,
    step_lines,
    usage_lines,
)


def test_step_lines_time_only_the_running_step() -> None:
    steps = (StepState(name="Lint", status="passed"), StepState(name="Tests", status="running"))
    assert step_lines(steps, "◜ ", 12) == ["✅ Lint", "◜  Tests · 12s"]


# The usage panel

TZ = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)


def usage_rows(*limits: UsageLimit, width: int = 10, **fields: object) -> list[str]:
    event = UsageChanged.model_validate({"limits": limits, **fields})
    return [line.plain for line in usage_lines(event, width, NOW)]


def bar_style(percent: float, status: str = "ok") -> str:
    event = UsageChanged.model_validate(
        {"limits": (UsageLimit(label="Week", percent=percent),), "status": status}
    )
    return str(usage_lines(event, 10, NOW)[1].style)


def test_a_limit_shows_its_header_and_bar() -> None:
    resets = datetime(2026, 10, 16, 19, 40, tzinfo=UTC)  # 3:40pm at NOW's offset
    limit = UsageLimit(label="Session", percent=42.4, resets_at=resets)
    assert usage_rows(limit)[:2] == ["Session  42%  resets 3:40pm", "████░░░░░░"]


def test_a_limit_without_a_reset_time_leaves_it_out_of_the_header() -> None:
    assert usage_rows(UsageLimit(label="Week", percent=7))[0] == "Week  7%"


def test_limits_are_shown_in_order() -> None:
    rows = usage_rows(UsageLimit(label="Session", percent=0), UsageLimit(label="Week", percent=0))
    assert [rows[0], rows[2]] == ["Session  0%", "Week  0%"]


def test_bar_fills_in_proportion_to_the_percent() -> None:
    def bar(percent: float, width: int = 10) -> str:
        return usage_rows(UsageLimit(label="Week", percent=percent), width=width)[1]

    assert bar(0) == "░" * 10
    assert bar(50) == "█" * 5 + "░" * 5
    assert bar(100) == "█" * 10
    assert bar(36) == "████░░░░░░"  # 3.6 cells round to 4
    assert bar(34) == "███░░░░░░░"


def test_bar_is_at_least_one_cell_wide() -> None:
    assert usage_rows(UsageLimit(label="Week", percent=100), width=1)[1] == "█"
    assert usage_rows(UsageLimit(label="Week", percent=0), width=0)[1] == "░"


def test_bar_colour_warns_as_the_limit_nears() -> None:
    assert bar_style(74.9) == "green"
    assert bar_style(75) == "yellow"
    assert bar_style(89.9) == "yellow"
    assert bar_style(90) == "red"


def test_stale_limits_are_dimmed() -> None:
    assert bar_style(95, "unavailable") == "dim"
    event = UsageChanged(limits=(UsageLimit(label="Week", percent=95),), status="rate_limited")
    assert [str(line.style) for line in usage_lines(event, 10, NOW)[:2]] == ["dim", "dim"]


def test_current_limits_have_a_plain_header_and_no_status_line() -> None:
    event = UsageChanged(limits=(UsageLimit(label="Week", percent=5),))
    lines = usage_lines(event, 10, NOW)
    assert len(lines) == 2
    assert str(lines[0].style) == ""


def test_rate_limited_usage_says_when_its_retried() -> None:
    retry = datetime(2026, 10, 16, 13, 5, tzinfo=TZ)
    rows = usage_rows(status="rate_limited", retry_at=retry)
    assert rows == ["usage: rate limited, retrying 1:05pm"]


def test_rate_limited_usage_without_a_retry_time_just_says_so() -> None:
    assert usage_rows(status="rate_limited") == ["usage: rate limited"]


def test_unavailable_usage_says_why() -> None:
    limit = UsageLimit(label="Week", percent=5)
    rows = usage_rows(limit, status="unavailable", detail="offline")
    assert rows[-1] == "usage unavailable: offline"


def test_unavailable_usage_without_a_detail_just_says_so() -> None:
    assert usage_rows(status="unavailable") == ["usage unavailable"]


def test_no_limits_reported_says_so() -> None:
    assert usage_rows() == ["no usage limits reported"]


def test_reset_label_on_the_same_day_is_the_time() -> None:
    assert reset_label(datetime(2026, 10, 16, 0, 5, tzinfo=TZ), NOW) == "12:05am"
    assert reset_label(datetime(2026, 10, 16, 12, 0, tzinfo=TZ), NOW) == "12:00pm"
    assert reset_label(datetime(2026, 10, 16, 23, 59, tzinfo=TZ), NOW) == "11:59pm"


def test_reset_label_on_another_day_names_the_day() -> None:
    assert reset_label(datetime(2026, 10, 19, 9, 0, tzinfo=TZ), NOW) == "Mon 9:00am"
    # Midnight UTC on the 17th is still the 16th at NOW's offset.
    assert reset_label(datetime(2026, 10, 17, 0, 0, tzinfo=UTC), NOW) == "8:00pm"
