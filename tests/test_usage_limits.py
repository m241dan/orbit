"""Usage limits and API outages: telling that an agent hit one, and when a run it stopped
starts again."""

from datetime import UTC, datetime, timedelta

import pytest

from orbit.agent.domain.runtimes import CLAUDE, KIRO, Failure, FailureKind
from orbit.agent.domain.usage_limits import (
    KIRO_LIMIT_RETRY,
    LimitWaits,
    Outage,
    RuntimeStop,
    api_unavailable,
    claude_failure,
    kiro_failure,
    limit_hit,
    limit_resume_time,
    outage_after,
    outage_delay,
    outage_paused_note,
    outage_resume_time,
    outage_resuming_note,
    paused_note,
    resume_time,
)
from orbit.protocol import UsageChanged, UsageLimit

NOW = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)
WAITS = LimitWaits(retry=timedelta(minutes=15), grace=timedelta(minutes=1))
FAKE = "fake"
"""A runtime other than Claude."""


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


def stop(
    kind: FailureKind, runtime: str = CLAUDE, resets_at: datetime | None = None
) -> RuntimeStop:
    """A run stopped by a failure of ``kind`` on ``runtime``, resetting at ``resets_at`` if
    given."""
    return RuntimeStop(runtime, Failure(kind, resets_at))


def test_a_limit_whose_failure_states_its_reset_starts_the_run_again_then() -> None:
    stated, shown = NOW + timedelta(hours=3), NOW + timedelta(hours=2)
    limit = stop("usage_limit", resets_at=stated)
    assert limit_resume_time(limit, used((100, shown)), NOW, WAITS) == stated


def test_a_limit_without_a_stated_reset_starts_again_as_its_runtimes_usage_says() -> None:
    shown = NOW + timedelta(hours=2)
    assert limit_resume_time(stop("usage_limit"), used((100, shown)), NOW, WAITS) == (
        shown + WAITS.grace
    )
    assert limit_resume_time(stop("usage_limit"), None, NOW, WAITS) == NOW + WAITS.retry


def test_a_stated_reset_already_past_is_left_for_the_usage() -> None:
    shown = NOW + timedelta(hours=2)
    limit = stop("usage_limit", resets_at=NOW - timedelta(minutes=1))
    assert limit_resume_time(limit, used((100, shown)), NOW, WAITS) == shown + WAITS.grace


def test_another_runtimes_usage_doesnt_time_a_limit_so_it_retries() -> None:
    claudes = used((100, NOW + timedelta(hours=2)))
    assert limit_resume_time(stop("usage_limit", FAKE), claudes, NOW, WAITS) == NOW + WAITS.retry


def test_a_limit_whose_runtime_declares_its_retry_retries_after_that() -> None:
    hourly = RuntimeStop(KIRO, Failure("usage_limit", retry=timedelta(hours=1)))
    assert limit_resume_time(hourly, None, NOW, WAITS) == NOW + timedelta(hours=1)


def test_a_declared_retry_still_gives_way_to_a_stated_reset() -> None:
    stated = NOW + timedelta(hours=3)
    limit = Failure("usage_limit", resets_at=stated, retry=timedelta(hours=1))
    assert limit_resume_time(RuntimeStop(KIRO, limit), None, NOW, WAITS) == stated


def test_an_outage_whose_failure_states_its_reset_starts_the_run_again_then() -> None:
    stated = NOW + timedelta(hours=1)
    outage = Outage(CLAUDE, "API Error: 529", level=3)
    assert outage_resume_time(stop("api_unavailable", resets_at=stated), outage, NOW) == stated


def test_an_outage_without_a_stated_reset_ahead_waits_out_the_backoff() -> None:
    outage = Outage(CLAUDE, "API Error: 529", level=1)
    past = stop("api_unavailable", resets_at=NOW)
    assert outage_resume_time(stop("api_unavailable"), outage, NOW) == NOW + outage_delay(1)
    assert outage_resume_time(past, outage, NOW) == NOW + outage_delay(1)


def test_the_pause_note_names_the_time_in_utc() -> None:
    at = datetime(2026, 10, 21, 14, 1, tzinfo=UTC) + timedelta(hours=4)
    assert paused_note(at, CLAUDE) == (
        "Claude's usage limit is reached, so the run stopped. It starts again by itself at "
        "Wed 21 Oct 18:01 UTC; ctrl+r starts it now and ctrl+s cancels that."
    )


def test_the_pause_note_names_another_runtime_by_its_name() -> None:
    at = datetime(2026, 10, 21, 18, 1, tzinfo=UTC)
    assert paused_note(at, FAKE).startswith("fake's usage limit is reached, so the run stopped.")


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
    first = outage_after(None, CLAUDE, "API Error: 529")
    assert first == Outage(CLAUDE, "API Error: 529", level=0)
    assert outage_after(first, CLAUDE, "Request timed out") == Outage(
        CLAUDE, "Request timed out", level=1
    )


def test_an_outage_on_another_runtime_starts_its_own_backoff() -> None:
    claudes = Outage(CLAUDE, "API Error: 529", level=2)
    assert outage_after(claudes, FAKE, "Gateway down") == Outage(FAKE, "Gateway down", level=0)


def test_the_outage_note_names_the_error_s_first_line_and_the_time_in_utc() -> None:
    at = datetime(2026, 10, 23, 14, 20, tzinfo=UTC)
    error = "\nAPI Error: 529 Overloaded\n  retrying later"
    assert outage_paused_note(at, error, CLAUDE) == (
        "Claude's API isn't answering (API Error: 529 Overloaded), so the run stopped. It starts "
        "again by itself at Fri 23 Oct 14:20 UTC; ctrl+r starts it now and ctrl+s cancels that."
    )


def test_the_outage_notes_name_another_runtime_by_its_name() -> None:
    at = datetime(2026, 10, 23, 14, 20, tzinfo=UTC)
    assert outage_paused_note(at, "Gateway down", FAKE).startswith(
        "fake's API isn't answering (Gateway down), so the run stopped."
    )
    assert outage_resuming_note(FAKE) == "Starting the run again now fake's API may be answering."
    assert outage_resuming_note(CLAUDE) == (
        "Starting the run again now Claude's API may be answering."
    )


KIRO_OUTAGE = (
    "Internal error (code -32603): Encountered an error in the response stream: An unknown error"
    " occurred: dispatch failure"
)
"""What Kiro says when it can't reach its service, as captured."""

KIRO_BAD_MODEL = (
    "Internal error (code -32603): Encountered an error in the response stream: The model"
    " 'no-such-model' is not available. Please use '/model' to select a different model and try"
    " again. (request_id: REQUEST)"
)
"""What Kiro says of a model it doesn't have, as captured."""


@pytest.mark.parametrize(
    "message",
    [
        KIRO_OUTAGE,
        "An unknown error occurred: DISPATCH FAILURE",
        "Service error: HTTP 503 Service Unavailable",
        "unexpected status code: 500",
        "Status: 502 Bad Gateway",
        "error code 529",
        "The model is overloaded, try again later",
        "ServiceOverloadedException: monthly credit limit",
    ],
)
def test_kiros_connection_failures_server_errors_and_overloads_are_outages(message: str) -> None:
    assert kiro_failure(message) == Failure("api_unavailable")


@pytest.mark.parametrize(
    "message",
    [
        "Monthly credit limit reached",
        "You have exceeded your QUOTA",
        "Out of Credits",
        "Rate LIMIT exceeded (HTTP 429)",
        "Quota exceeded",
        "You have run out of credits",
        "Usage limits reached",
        "Request rate-limited",
    ],
)
def test_kiros_limits_quotas_and_credits_are_usage_limits_retried_hourly(message: str) -> None:
    assert kiro_failure(message) == Failure("usage_limit", retry=KIRO_LIMIT_RETRY)
    assert timedelta(hours=1) == KIRO_LIMIT_RETRY


@pytest.mark.parametrize(
    "message",
    [
        KIRO_BAD_MODEL,
        "Kiro couldn't connect to orbit's MCP server at http://127.0.0.1:500/mcp",
        "HTTP 404 Not Found",
        "status 5000",
        "Invalid delimiter in tool input",
        "unlimited context",
        "Accredited source",
        "",
    ],
)
def test_kiros_other_errors_are_no_failure(message: str) -> None:
    assert kiro_failure(message) is None


def test_the_notes_name_kiro_by_its_name() -> None:
    at = datetime(2026, 10, 23, 14, 20, tzinfo=UTC)
    assert paused_note(at, KIRO).startswith("Kiro's usage limit is reached, so the run stopped.")
    assert outage_resuming_note(KIRO) == "Starting the run again now Kiro's API may be answering."
