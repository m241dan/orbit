from datetime import UTC, datetime, timedelta, timezone

from orbit.protocol import (
    PAUSED_FOR_API_UNAVAILABLE,
    PAUSED_FOR_USAGE_LIMIT,
    ActivateSession,
    Activity,
    Attach,
    NewSession,
    SessionSummary,
    Usage,
    WorkspaceEntry,
)
from orbit.tui.view.common import picker_hint
from orbit.tui.view.sessions import (
    ACTIVATE_CHOICE,
    JUST_LOOK_CHOICE,
    LIST_HINT,
    NAME_HINT,
    RENAME_HINT,
    SESSIONS_HINTS,
    STARTUP_HINT,
    PickerRow,
    Viewing,
    activation_choices,
    activation_message,
    active_name,
    activity_words,
    can_delete,
    can_open,
    chosen_workspace,
    delete_message,
    held_back,
    idle_now_message,
    local_time,
    name_hint,
    opening_attach,
    paused_words,
    picked_commands,
    picker_rows,
    picker_sessions,
    picker_viewing,
    running_words,
    session_label,
    session_turned_idle,
    state_notes,
    task_counts,
    unjoined_view,
    viewed_only,
    viewing_mark,
    waiting_words,
    workspace_sessions,
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


WAITING_SECOND = (
    Activity(kind="task_run", task_id="t1"),
    Activity(kind="waiting_for_coder", position=2, busy=4),
)
"""A task run whose task waits second in line for a coder."""


def test_a_session_waiting_for_a_coder_says_so_rather_than_running() -> None:
    summary = SessionSummary(
        id="s", name="today", active=True, state="running", activity=WAITING_SECOND
    )
    assert state_notes(summary) == ["active", "waiting for a coder: 2nd in line"]
    assert session_label(summary, UTC).plain == (
        "  today · active · waiting for a coder: 2nd in line"
    )


TWO_AHEAD = timezone(timedelta(hours=2))
"""A local zone two hours ahead of UTC."""


def paused(reason: str | None, until: datetime) -> SessionSummary:
    """The active session, its run paused for ``reason`` until ``until``."""
    return SessionSummary(id="s", name="today", active=True, paused_until=until, paused_for=reason)


def test_a_run_an_outage_paused_says_when_it_tries_again_in_local_time() -> None:
    summary = paused(PAUSED_FOR_API_UNAVAILABLE, datetime(2026, 10, 23, 12, 20, tzinfo=UTC))
    assert paused_words(summary, TWO_AHEAD) == (
        "run paused: Claude's API isn't answering; trying again at 14:20"
    )


def test_a_run_the_usage_limit_paused_says_when_it_starts_again() -> None:
    summary = paused(PAUSED_FOR_USAGE_LIMIT, datetime(2026, 10, 23, 16, 1, tzinfo=UTC))
    assert paused_words(summary, TWO_AHEAD) == "run paused: usage limit; starts again at 18:01"


def test_a_run_paused_for_a_reason_not_known_says_only_when_it_starts_again() -> None:
    until = datetime(2026, 10, 23, 12, 20, tzinfo=UTC)
    words = [paused_words(paused(reason, until), TWO_AHEAD) for reason in ("maintenance", None)]
    assert words == ["run paused; starts again at 14:20"] * 2


def test_a_run_not_paused_has_no_words_for_it() -> None:
    assert paused_words(SessionSummary(id="s", name="today")) is None


def test_a_paused_sessions_row_and_state_notes_say_its_run_is_paused() -> None:
    summary = paused(PAUSED_FOR_USAGE_LIMIT, datetime(2026, 10, 23, 16, 1, tzinfo=UTC))
    words = "run paused: usage limit; starts again at 18:01"
    assert state_notes(summary, zone=TWO_AHEAD) == ["active", words]
    assert session_label(summary, TWO_AHEAD).plain == f"  today · active · {words}"


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
    hint = picker_hint(SESSIONS_HINTS, startup=False, box=None)
    assert hint == LIST_HINT
    assert hint.endswith("esc close")


def test_at_startup_the_list_hint_says_escape_quits() -> None:
    hint = picker_hint(SESSIONS_HINTS, startup=True, box=None)
    assert hint == STARTUP_HINT
    assert hint.endswith("esc quit")


def test_naming_a_new_session_hints_enter_starts_it() -> None:
    assert name_hint(renaming=False) == NAME_HINT
    assert NAME_HINT.startswith("enter start")


def test_renaming_a_session_hints_enter_renames_it() -> None:
    assert name_hint(renaming=True) == RENAME_HINT
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
    assert picked_commands(NOW, (NOW, OLD), "now", "w") == ()


def test_picking_with_the_active_session_idle_and_viewed_only_here_activates() -> None:
    assert picked_commands(OLD, (NOW, OLD), "now", "w") == (
        Attach(workspace="w", session="old"),
        ActivateSession(session="old"),
    )


def test_picking_while_the_active_session_runs_only_attaches() -> None:
    running = NOW.model_copy(update={"state": "running"})
    assert picked_commands(OLD, (running, OLD), "now", "w") == (
        Attach(workspace="w", session="old"),
    )


def test_picking_while_another_frontend_views_the_active_session_only_attaches() -> None:
    viewed = NOW.model_copy(update={"viewers": 2})
    assert picked_commands(OLD, (viewed, OLD), "now", "w") == (
        Attach(workspace="w", session="old"),
    )


def test_a_frontend_viewing_another_session_counts_every_viewer_of_the_active_one() -> None:
    assert picked_commands(OTHER, (NOW, OLD, OTHER), "old", "w") == (
        Attach(workspace="w", session="other"),
    )
    unviewed = NOW.model_copy(update={"viewers": 0})
    assert picked_commands(OTHER, (unviewed, OLD, OTHER), "old", "w") == (
        Attach(workspace="w", session="other"),
        ActivateSession(session="other"),
    )


def test_picking_the_active_session_from_another_only_attaches() -> None:
    assert picked_commands(NOW, (NOW, OLD), "old", "w") == (Attach(workspace="w", session="now"),)


def test_with_no_active_session_picking_activates() -> None:
    assert picked_commands(OLD, (OLD,), None, "w") == (
        Attach(workspace="w", session="old"),
        ActivateSession(session="old"),
    )


def test_the_viewing_mark_names_what_the_active_session_runs() -> None:
    assert viewing_mark("old", "today", "running task 3") == (
        "viewing old; today is running task 3 (enter to switch when it's idle)"
    )


def test_the_viewing_mark_says_when_the_active_session_is_idle() -> None:
    assert viewing_mark("old", "today", None) == "viewing old; today is idle (enter to switch)"


def test_the_viewing_mark_without_a_known_active_session_says_how_to_switch() -> None:
    assert viewing_mark("auth rework", None, None) == (
        "viewing auth rework, not the active session (enter to switch)"
    )


def test_a_task_run_is_a_task() -> None:
    assert activity_words((Activity(kind="task_run", task_id="t1"),)) == "a task"


def test_several_activities_are_joined() -> None:
    activity = (Activity(kind="planner"), Activity(kind="review", task_id="t5"))
    assert activity_words(activity) == "the planner and the review of a task"


def test_more_than_two_activities_are_joined_by_commas_then_and() -> None:
    activity = (
        Activity(kind="startup_check"),
        Activity(kind="task_run", task_id="t1"),
        Activity(kind="planner"),
    )
    assert activity_words(activity) == "the startup check, a task and the planner"


def test_no_activity_is_work() -> None:
    assert activity_words(()) == "work"


def test_with_the_active_session_idle_the_prompt_offers_to_activate_or_just_look() -> None:
    assert activation_choices(running=False) == (ACTIVATE_CHOICE, JUST_LOOK_CHOICE)
    assert ACTIVATE_CHOICE == "Make this the active session"
    assert JUST_LOOK_CHOICE == "Just look"
    assert activation_message("auth rework", "today", running=None) == (
        "auth rework is a read-only view; today is the active session, which runs work."
    )


def test_with_the_active_session_running_the_prompt_only_offers_to_just_look() -> None:
    assert activation_choices(running=True) == (JUST_LOOK_CHOICE,)
    assert activation_message("auth rework", "today", running="running task 3") == (
        "today is running task 3; you can switch once it's idle."
    )


def test_the_prompt_again_says_the_active_session_is_idle_now() -> None:
    assert idle_now_message("today") == "today is idle now."


def test_what_the_active_session_runs_is_worded_while_its_state_says_it_runs() -> None:
    task = (Activity(kind="task_run", task_id="t1"),)
    idle = SessionSummary(id="s", name="today", active=True, activity=task)
    running = idle.model_copy(update={"state": "running"})
    other = SessionSummary(id="o", name="auth rework", state="running")
    assert running_words((other, running)) == "running a task"
    assert running_words((other, idle)) is None
    assert running_words(()) is None


def test_a_wait_for_a_coder_is_worded_with_its_place_in_line() -> None:
    assert waiting_words(1) == "waiting for a coder: next in line"
    assert waiting_words(2) == "waiting for a coder: 2nd in line"
    assert waiting_words(3) == "waiting for a coder: 3rd in line"
    assert waiting_words(4) == "waiting for a coder: 4th in line"
    assert waiting_words(11) == "waiting for a coder: 11th in line"
    assert waiting_words(12) == "waiting for a coder: 12th in line"
    assert waiting_words(13) == "waiting for a coder: 13th in line"
    assert waiting_words(21) == "waiting for a coder: 21st in line"
    assert waiting_words(22) == "waiting for a coder: 22nd in line"
    assert waiting_words(None) == "waiting for a coder"


def test_an_active_session_waiting_for_a_coder_is_worded_as_waiting() -> None:
    waiting = SessionSummary(
        id="s", name="today", active=True, state="running", activity=WAITING_SECOND
    )
    assert running_words((waiting,)) == "waiting for a coder: 2nd in line"


def test_what_else_runs_while_waiting_for_a_coder_comes_first() -> None:
    activity = (*WAITING_SECOND, Activity(kind="planner"))
    waiting = SessionSummary(id="s", name="today", active=True, state="running", activity=activity)
    assert running_words((waiting,)) == ("running the planner and waiting for a coder: 2nd in line")


def test_attaching_alone_to_an_inactive_session_views_it_only() -> None:
    sessions = (
        SessionSummary(id="now", name="today", active=True),
        SessionSummary(id="o", name="x"),
    )
    assert viewed_only((Attach(workspace="w", session="o"),), sessions) == "o"
    assert viewed_only((Attach(workspace="w", session="now"),), sessions) is None
    assert (
        viewed_only((Attach(workspace="w", session="o"), ActivateSession(session="o")), sessions)
        is None
    )
    assert viewed_only((NewSession(),), sessions) is None
    assert viewed_only((), sessions) is None


def test_the_active_session_is_named_as_the_sessions_name_it() -> None:
    idle = SessionSummary(id="s", name="today", active=True)
    other = SessionSummary(id="o", name="auth rework")
    assert active_name((other, idle)) == "today"
    assert active_name((other,)) is None


ONE = WorkspaceEntry(id="w1", label="one", path="/src/one", sessions=())
TWO = WorkspaceEntry(
    id="w2", label="two", path="/src/two", sessions=(SessionSummary(id="s", name="s"),)
)


def test_the_workspace_opened_on_is_the_one_at_the_repository() -> None:
    assert chosen_workspace((ONE, TWO), "/src/two") == TWO


def test_the_workspace_opened_on_is_else_the_first() -> None:
    assert chosen_workspace((ONE, TWO), "/src/three") == ONE
    assert chosen_workspace((ONE, TWO), None) == ONE
    assert chosen_workspace((), "/src/one") is None


def test_a_workspaces_sessions_are_those_its_entry_lists() -> None:
    assert workspace_sessions((ONE, TWO), "w2") == TWO.sessions
    assert workspace_sessions((ONE, TWO), "w3") == ()
    assert workspace_sessions((ONE, TWO), None) == ()


def test_opening_attaches_to_the_active_session_of_the_workspace() -> None:
    active = SessionSummary(id="a", name="a", active=True)
    others = (SessionSummary(id="s", name="s"), active)
    assert opening_attach("w1", others) == Attach(workspace="w1", session="a")
    assert opening_attach(None, others) is None
    assert opening_attach("w1", others[:1]) is None


def test_the_picker_marks_the_session_viewed_first() -> None:
    rows = picker_rows((OLD, NOW), Viewing("w", "now"), startup=False, resume=False)
    assert rows == (PickerRow(NOW, attached=True), PickerRow(OLD))


def test_the_picker_at_startup_offers_to_resume_the_session_viewed() -> None:
    rows = picker_rows((NOW, OLD), Viewing("w", "now"), startup=True, resume=True)
    assert rows == (PickerRow(NOW, attached=True, resume=True), PickerRow(OLD))


def test_the_picker_at_startup_only_to_pick_leaves_out_the_session_viewed() -> None:
    rows = picker_rows((NOW, OLD), Viewing("w", "now"), startup=True, resume=False)
    assert rows == (PickerRow(OLD),)


def test_the_picker_of_a_workspace_not_joined_offers_its_active_session_unmarked() -> None:
    viewing = Viewing("w", "now", joined=False)
    rows = picker_rows((OLD, NOW), viewing, startup=False, resume=False)
    assert rows == (PickerRow(NOW, resume=True), PickerRow(OLD))


IDLE_NOW = SessionSummary(id="now", name="today", active=True, state="idle")
RUNNING_NOW = SessionSummary(id="now", name="today", active=True, state="running")


def test_a_session_held_back_by_an_idle_active_one_names_that_one() -> None:
    assert session_turned_idle((OLD, IDLE_NOW), "old") == "today"


def test_a_running_active_session_has_not_turned_idle() -> None:
    assert session_turned_idle((OLD, RUNNING_NOW), "old") is None


def test_the_held_session_turned_active_has_not_turned_idle() -> None:
    assert session_turned_idle((OLD, IDLE_NOW), "now") is None


def test_with_no_active_session_none_turned_idle() -> None:
    assert session_turned_idle((OLD, OTHER), "old") is None


def test_a_running_active_session_holds_back_the_session_viewed() -> None:
    assert held_back("old", running="a task", active="today") == "old"


def test_an_idle_active_session_holds_back_nothing() -> None:
    assert held_back("old", running=None, active="today") is None


def test_an_unknown_active_session_holds_back_nothing() -> None:
    assert held_back("old", running="a task", active=None) is None


IN_W = WorkspaceEntry(id="w", label="w", sessions=(OLD, NOW))
IN_V = WorkspaceEntry(id="v", label="v", sessions=(OTHER, IDLE_NOW))


def test_another_workspace_is_viewed_unjoined_at_its_active_session() -> None:
    viewing, sessions = unjoined_view((IN_W, IN_V), "v")
    assert viewing == Viewing("v", "now", joined=False)
    assert sessions == (OTHER, IDLE_NOW)


def test_the_picker_of_the_workspace_it_is_on_views_what_this_frontend_views() -> None:
    here = picker_viewing((IN_W, IN_V), "w", "old", (OLD,), "w")
    assert here == (Viewing("w", "old"), (OLD,))


def test_the_picker_views_the_workspace_it_is_on_by_default() -> None:
    assert picker_viewing((IN_W,), "w", "old", (OLD,), None) == (Viewing("w", "old"), (OLD,))


def test_the_picker_before_any_workspace_views_none() -> None:
    assert picker_viewing((), None, None, (), None) == (Viewing("", None), ())


def test_the_picker_of_another_workspace_views_it_unjoined() -> None:
    viewing, sessions = picker_viewing((IN_W, IN_V), "w", "old", (OLD,), "v")
    assert viewing == Viewing("v", "now", joined=False)
    assert sessions == (OTHER, IDLE_NOW)
