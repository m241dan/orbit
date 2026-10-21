"""Usage limits: telling that an agent hit one, and when a run it stopped starts again."""

from datetime import UTC, datetime, timedelta

from orbit.agent.domain.usage_limits import LimitWaits, limit_hit, paused_note, resume_time
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
