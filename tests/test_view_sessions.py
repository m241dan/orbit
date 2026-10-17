from datetime import UTC, datetime, timedelta, timezone

from orbit.protocol import SessionSummary, Usage
from orbit.tui.view import (
    can_delete,
    can_open,
    delete_message,
    local_time,
    session_label,
    task_counts,
)

ACTIVE = datetime(2026, 10, 16, 18, 2, tzinfo=UTC)


def test_a_session_row_shows_name_last_activity_task_counts_and_cost() -> None:
    summary = SessionSummary(
        id="s",
        name="auth rework",
        last_active=ACTIVE,
        counts={"draft": 1, "pending": 2, "completed": 3},
        usage=Usage(input_tokens=1_200_000, cost_usd=0.84),
    )
    label = session_label(summary, UTC)
    assert label.plain == "  auth rework · Oct 16 18:02 · 3 ✅ 2 ⏳ 1 📝 · 1.2M tok · $0.84"
    assert label.style == ""


def test_the_current_session_is_marked() -> None:
    summary = SessionSummary(id="s", name="auth rework", last_active=ACTIVE, current=True)
    assert session_label(summary, UTC).plain == "● auth rework · Oct 16 18:02"


def test_a_session_never_active_shows_only_its_name() -> None:
    assert session_label(SessionSummary(id="s", name="Oct 17 14:30"), UTC).plain == (
        "  Oct 17 14:30"
    )


def test_a_locked_session_says_so_dimmed() -> None:
    summary = SessionSummary(
        id="s", name="auth rework", last_active=ACTIVE, counts={"pending": 2}, locked=True
    )
    label = session_label(summary, UTC)
    assert label.plain == "  auth rework · Oct 16 18:02 · open in another orbit"
    assert label.style == "dim"


def test_an_unreadable_session_gives_the_reason_dimmed() -> None:
    summary = SessionSummary(id="s", name="s.json", unreadable="not JSON")
    label = session_label(summary, UTC)
    assert label.plain == "  s.json · unreadable: not JSON"
    assert label.style == "dim"


def test_only_readable_unlocked_sessions_can_be_opened() -> None:
    assert can_open(SessionSummary(id="s", name="a"))
    assert can_open(SessionSummary(id="s", name="a", current=True))
    assert not can_open(SessionSummary(id="s", name="a", locked=True))
    assert not can_open(SessionSummary(id="s", name="a", unreadable="gone"))


def test_only_sessions_open_nowhere_can_be_deleted() -> None:
    assert can_delete(SessionSummary(id="s", name="a"))
    assert can_delete(SessionSummary(id="s", name="a", unreadable="gone"))
    assert not can_delete(SessionSummary(id="s", name="a", current=True))
    assert not can_delete(SessionSummary(id="s", name="a", locked=True))


def test_deleting_asks_about_the_session_by_name() -> None:
    assert delete_message(SessionSummary(id="s", name="auth rework")) == (
        'Delete "auth rework"? Its tasks and review state are gone for good.'
    )


def test_last_activity_is_shown_in_the_zone_given() -> None:
    assert local_time(ACTIVE, timezone(timedelta(hours=-7))) == "Oct 16 11:02"


def test_last_activity_defaults_to_the_local_zone() -> None:
    assert local_time(ACTIVE) == local_time(ACTIVE, ACTIVE.astimezone().tzinfo)


def test_task_counts_leave_out_none_and_put_finished_work_first() -> None:
    counts = task_counts({"draft": 1, "running": 1, "error": 0, "cancelled": 2, "gave_up": 1})
    assert counts == "1 🏳 2 🛑 1 ◜ 1 📝"


def test_no_tasks_count_nothing() -> None:
    assert task_counts({}) == ""
