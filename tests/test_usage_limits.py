"""Usage limits and API outages: telling that an agent hit one, and when a run it stopped
starts again."""

from datetime import UTC, datetime, timedelta

from orbit.agent.domain.runtimes import Failure
from orbit.agent.domain.usage_limits import (
    LimitWaits,
    Outage,
    api_unavailable,
    claude_failure,
    limit_hit,
    outage_after,
    outage_delay,
    outage_paused_note,
    paused_note,
    resume_time,
)
from orbit.protocol import UsageChanged, UsageLimit

NOW = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)
WAITS = LimitWaits(retry=timedelta(minutes=15), grace=timedelta(minutes=1))


def used(*limits: tuple[float, datetime | None]) -> UsageChanged:
    """Usage with a limit per ``(percent, resets_at)`` given."""
    return UsageChanged(
        limits=tuple(
            UsageLimit(label=f"Limit {n}", percent=percent, resets_at=resets_at)
            for n, (percent, resets_at) in enumerate(limits)
        )
    )


def test_an_agent_hitting_a_session_or_weekly_limit_says_so() -> None:
    assert limit_hit("You've hit your session limit · resets 6pm (UTC)")
    assert limit_hit("You've hit your weekly limit · resets Oct 24, 5pm (UTC)")
    assert limit_hit("Claude AI usage limit reached|1792540800")
    assert limit_hit("you've HIT YOUR Opus LIMIT")


def test_other_failures_are_not_limits() -> None:
    assert not limit_hit("agent-1 exited without calling get_task.")
    assert not limit_hit("Rate limited by the proxy")
    assert not limit_hit("")


def test_without_usage_a_run_starts_again_after_the_retry() -> None:
    assert resume_time(None, NOW, WAITS) == NOW + timedelta(minutes=15)


def test_a_used_up_limit_starts_the_run_again_a_little_after_it_resets() -> None:
    resets = NOW + timedelta(hours=2)
    assert resume_time(used((100, resets), (40, None)), NOW, WAITS) == resets + WAITS.grace


def test_with_several_limits_used_up_the_run_waits_for_the_last_to_reset() -> None:
    session, week = NOW + timedelta(hours=2), NOW + timedelta(days=3)
    usage = used((100, week), (100, session))
    assert resume_time(usage, NOW, WAITS) == week + WAITS.grace


def test_a_limit_not_used_up_yet_or_without_a_reset_time_means_retrying() -> None:
    retry = NOW + WAITS.retry
    assert resume_time(used((99.5, NOW + timedelta(hours=1))), NOW, WAITS) == retry
    assert resume_time(used((100, None)), NOW, WAITS) == retry


def test_a_reset_already_past_means_retrying() -> None:
    usage = used((100, NOW - timedelta(minutes=5)))
    assert resume_time(usage, NOW, WAITS) == NOW + WAITS.retry


def test_the_waits_default_to_fifteen_minutes_and_one_of_grace() -> None:
    assert LimitWaits() == WAITS


def test_the_pause_note_names_the_time_in_utc() -> None:
    at = datetime(2026, 10, 21, 14, 1, tzinfo=UTC) + timedelta(hours=4)
    assert paused_note(at) == (
        "Claude's usage limit is reached, so the run stopped. It starts again by itself at "
        "Wed 21 Oct 18:01 UTC; ctrl+r starts it now and ctrl+s cancels that."
    )


def test_claude_code_api_errors_for_an_unavailable_api_say_so() -> None:
    assert api_unavailable('API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}')
    assert api_unavailable("API Error: 503 Service Unavailable")
    assert api_unavailable("API Error (Connection error.)")
    assert api_unavailable("Request timed out")


def test_every_server_error_and_an_overload_say_so_in_any_case() -> None:
    for code in ("500", "502", "503", "504", "529"):
        assert api_unavailable(f"API Error: {code} upstream")
    assert api_unavailable("API Error: OVERLOADED_ERROR")
    assert api_unavailable("api error (connection ERROR.)")
    assert api_unavailable("request TIMED OUT after 600s")


def test_client_errors_limits_and_other_failures_are_not_outages() -> None:
    assert not api_unavailable('API Error: 400 {"error":{"type":"invalid_request_error"}}')
    assert not api_unavailable('API Error: 401 {"error":{"type":"authentication_error"}}')
    assert not api_unavailable("API Error: 403 Forbidden")
    assert not api_unavailable("You've hit your session limit")
    assert not api_unavailable("agent-1 exited without calling get_task.")
    assert not api_unavailable("")


def test_claude_codes_limits_and_outages_are_failures_of_their_kind_with_no_reset_time() -> None:
    assert claude_failure("Claude AI usage limit reached|1792540800") == Failure("usage_limit")
    assert claude_failure("API Error (Connection error.)") == Failure("api_unavailable")


def test_claude_codes_other_errors_are_no_failure() -> None:
    assert claude_failure("API Error: 403 Forbidden") is None
    assert claude_failure("agent-1 exited without calling get_task.") is None


def test_the_backoff_grows_to_half_an_hour_and_stays_there() -> None:
    minutes = [outage_delay(level) / timedelta(minutes=1) for level in range(6)]
    assert minutes == [2, 5, 10, 20, 30, 30]


def test_a_first_outage_starts_the_backoff_and_each_after_it_goes_a_level_up() -> None:
    first = outage_after(None, "API Error: 529")
    assert first == Outage("API Error: 529", level=0)
    assert outage_after(first, "Request timed out") == Outage("Request timed out", level=1)


def test_the_outage_note_names_the_error_s_first_line_and_the_time_in_utc() -> None:
    at = datetime(2026, 10, 23, 14, 20, tzinfo=UTC)
    error = "\nAPI Error: 529 Overloaded\n  retrying later"
    assert outage_paused_note(at, error) == (
        "Claude's API isn't answering (API Error: 529 Overloaded), so the run stopped. It starts "
        "again by itself at Fri 23 Oct 14:20 UTC; ctrl+r starts it now and ctrl+s cancels that."
    )
