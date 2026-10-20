from datetime import UTC, datetime, timedelta, timezone

from orbit.protocol import (
    ActivateSession,
    Attach,
    NewSession,
    SessionSummary,
    Usage,
)
from orbit.tui.view.sessions import (
    ACTIVATE_CHOICE,
    JUST_LOOK_CHOICE,
    LIST_HINT,
    NAME_HINT,
    RENAME_HINT,
    STARTUP_HINT,
    activation_choices,
    activation_message,
    active_name,
    active_running,
    can_delete,
    can_open,
    delete_message,
    local_time,
    picked_commands,
    picker_sessions,
    session_label,
    sessions_hint,
    state_notes,
    task_counts,
    viewed_only,
    viewing_mark,
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


def test_the_session_viewed_is_marked() -> None:
    summary = SessionSummary(id="s", name="auth rework", last_active=ACTIVE, current=True)
    assert session_label(summary, UTC).plain == "  auth rework · Oct 16 18:02"
    assert session_label(summary, UTC, attached=True).plain == "● auth rework · Oct 16 18:02"


def test_the_session_to_resume_says_so() -> None:
    summary = SessionSummary(id="s", name="auth rework")
    assert session_label(summary, UTC, attached=True, resume=True).plain == ("● Resume auth rework")


def test_a_row_marks_the_active_running_session_and_its_other_viewers() -> None:
    summary = SessionSummary(
        id="s",
        name="auth rework",
        last_active=ACTIVE,
        counts={"running": 1},
        active=True,
        state="running",
        viewers=2,
    )
    assert session_label(summary, UTC).plain == (
        "  auth rework · Oct 16 18:02 · active · running · viewed by 2 · 1 ◜"
    )


def test_the_session_viewed_counts_only_the_other_viewers() -> None:
    assert state_notes(SessionSummary(id="s", name="a", viewers=2), attached=True) == [
        "viewed by 1"
    ]
    assert state_notes(SessionSummary(id="s", name="a", viewers=1), attached=True) == []


def test_an_idle_session_viewed_by_none_has_no_state_notes() -> None:
    assert state_notes(SessionSummary(id="s", name="a")) == []


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
    assert not can_delete(SessionSummary(id="s", name="a", current=True, active=True))
    assert not can_delete(SessionSummary(id="s", name="a", locked=True))


def test_a_session_once_open_here_but_no_longer_active_can_be_deleted() -> None:
    assert can_delete(SessionSummary(id="s", name="a", current=True, active=False))


def test_the_active_session_cannot_be_deleted() -> None:
    assert not can_delete(SessionSummary(id="s", name="a", active=True))


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


def test_the_list_hint_says_escape_closes_it() -> None:
    hint = sessions_hint(startup=False, naming=False, renaming=False)
    assert hint == LIST_HINT
    assert hint.endswith("esc close")


def test_at_startup_the_list_hint_says_escape_quits() -> None:
    hint = sessions_hint(startup=True, naming=False, renaming=False)
    assert hint == STARTUP_HINT
    assert hint.endswith("esc quit")


def test_naming_a_new_session_hints_enter_starts_it() -> None:
    assert sessions_hint(startup=True, naming=True, renaming=False) == NAME_HINT
    assert NAME_HINT.startswith("enter start")


def test_renaming_a_session_hints_enter_renames_it() -> None:
    assert sessions_hint(startup=False, naming=True, renaming=True) == RENAME_HINT
    assert RENAME_HINT.startswith("enter rename")


NOW = SessionSummary(id="now", name="today", active=True, viewers=1)
OLD = SessionSummary(id="old", name="auth rework")
OTHER = SessionSummary(id="other", name="flags")


def test_the_picker_lists_the_session_viewed_first_then_the_others_in_order() -> None:
    listed = picker_sessions((OLD, NOW, OTHER), "now", with_attached=True)
    assert [summary.id for summary in listed] == ["now", "old", "other"]


def test_the_picker_can_leave_out_the_session_viewed() -> None:
    listed = picker_sessions((NOW, OLD), "now", with_attached=False)
    assert [summary.id for summary in listed] == ["old"]


def test_with_no_session_viewed_the_picker_lists_them_all() -> None:
    assert picker_sessions((NOW, OLD), None, with_attached=True) == (NOW, OLD)


def test_picking_the_session_viewed_sends_nothing() -> None:
    assert picked_commands(NOW, (NOW, OLD), "now") == ()


def test_picking_with_the_active_session_idle_and_viewed_only_here_activates() -> None:
    assert picked_commands(OLD, (NOW, OLD), "now") == (
        Attach(session="old"),
        ActivateSession(session="old"),
    )


def test_picking_while_the_active_session_runs_only_attaches() -> None:
    running = NOW.model_copy(update={"state": "running"})
    assert picked_commands(OLD, (running, OLD), "now") == (Attach(session="old"),)


def test_picking_while_another_frontend_views_the_active_session_only_attaches() -> None:
    viewed = NOW.model_copy(update={"viewers": 2})
    assert picked_commands(OLD, (viewed, OLD), "now") == (Attach(session="old"),)


def test_a_frontend_viewing_another_session_counts_every_viewer_of_the_active_one() -> None:
    assert picked_commands(OTHER, (NOW, OLD, OTHER), "old") == (Attach(session="other"),)
    unviewed = NOW.model_copy(update={"viewers": 0})
    assert picked_commands(OTHER, (unviewed, OLD, OTHER), "old") == (
        Attach(session="other"),
        ActivateSession(session="other"),
    )


def test_picking_the_active_session_from_another_only_attaches() -> None:
    assert picked_commands(NOW, (NOW, OLD), "old") == (Attach(session="now"),)


def test_with_no_active_session_picking_activates() -> None:
    assert picked_commands(OLD, (OLD,), None) == (
        Attach(session="old"),
        ActivateSession(session="old"),
    )


def test_the_viewing_mark_names_the_session_and_how_to_switch() -> None:
    assert viewing_mark("auth rework") == (
        "viewing auth rework, not the active session (enter to switch)"
    )


def test_with_the_active_session_idle_the_prompt_offers_to_activate_or_just_look() -> None:
    assert activation_choices(running=False) == (ACTIVATE_CHOICE, JUST_LOOK_CHOICE)
    assert ACTIVATE_CHOICE == "Make this the active session"
    assert JUST_LOOK_CHOICE == "Just look"
    assert activation_message("auth rework", "today", running=False) == (
        "auth rework is a read-only view; today is the active session, which runs work."
    )


def test_with_the_active_session_running_the_prompt_only_offers_to_just_look() -> None:
    assert activation_choices(running=True) == (JUST_LOOK_CHOICE,)
    assert activation_message("auth rework", "today", running=True) == (
        "today is running; you can switch once it's idle."
    )


def test_the_active_session_runs_when_its_state_says_so() -> None:
    idle = SessionSummary(id="s", name="today", active=True)
    running = idle.model_copy(update={"state": "running"})
    other = SessionSummary(id="o", name="auth rework", state="running")
    assert active_running((other, running))
    assert not active_running((other, idle))
    assert not active_running(())


def test_attaching_alone_to_an_inactive_session_views_it_only() -> None:
    sessions = (
        SessionSummary(id="now", name="today", active=True),
        SessionSummary(id="o", name="x"),
    )
    assert viewed_only((Attach(session="o"),), sessions) == "o"
    assert viewed_only((Attach(session="now"),), sessions) is None
    assert viewed_only((Attach(session="o"), ActivateSession(session="o")), sessions) is None
    assert viewed_only((NewSession(),), sessions) is None
    assert viewed_only((), sessions) is None


def test_the_active_session_is_named_as_the_sessions_name_it() -> None:
    idle = SessionSummary(id="s", name="today", active=True)
    other = SessionSummary(id="o", name="auth rework")
    assert active_name((other, idle)) == "today"
    assert active_name((other,)) is None
