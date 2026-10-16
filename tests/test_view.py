import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, get_args

import pytest

from orbit.agent.domain.roles import BUILT_IN_ROLE_NAMES
from orbit.protocol import (
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
    Event,
    Notice,
    ProgressNoted,
    ReviewStatus,
    Settings,
    SettingsChanged,
    StepState,
    TaskGaveUp,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    TaskStatus,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    UsageChanged,
    UsageLimit,
    VerificationChanged,
    VerificationFinished,
    VerificationStarted,
)
from orbit.tui.view import (
    ALL_TAB_LABEL,
    GUTTER_BAR,
    GUTTER_MARKER,
    PINNED_TABS,
    PLANNER,
    ROLE_TABS,
    STATUS_ICONS,
    AgentRow,
    ConversationTab,
    activity_entry,
    agent_entry,
    agent_labelled,
    agent_line,
    coalesce,
    collapsible,
    entry_label,
    exchange_entry,
    gutter,
    join_prompt,
    mark_planner_busy,
    mark_tab_read,
    mark_tab_unread,
    model_options,
    note_entry,
    output_entry,
    reset_label,
    review_line,
    settings_summary,
    split_prompt,
    starts_collapsed,
    step_lines,
    tab_key,
    tab_labels,
    task_started_entry,
    task_tab_key,
    tool_access_labels,
    tool_call_summary,
    track_agents,
    track_roles,
    track_tabs,
    turn_end_entry,
    usage_lines,
    verification_finished_entry,
    verification_skipped_entry,
    verification_started_entry,
)


def test_turn_output_becomes_transcript_entries() -> None:
    assert output_entry(AssistantMessage(turn_id="t", text="hi")) == ("hi", "assistant")
    started = ToolCallStarted(turn_id="t", call_id="c", name="Read", arguments={"file_path": "a"})
    assert output_entry(started) == ('→ Read a\n{\n  "file_path": "a"\n}', "tool")
    assert output_entry(ToolCallFinished(turn_id="t", call_id="c", output="ok")) == ("ok", "tool")
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True)
    assert output_entry(failed) == ("no", "error")


def test_only_unexpected_turn_endings_are_noted() -> None:
    assert turn_end_entry("completed") is None
    assert turn_end_entry("error") is None  # its error message was shown already
    assert turn_end_entry("cancelled") == ("[turn ended: cancelled]", "tool")
    assert turn_end_entry("max_steps") == ("[turn ended: max_steps]", "tool")


def test_task_progress_entries() -> None:
    state = TaskState(id="7", task=TaskSpec(prompt="do it"))
    assert task_started_entry("1.2", state) == ("Task 1.2: do it", "user")
    # A task that isn't on the list has no prompt to show, only its id.
    assert task_started_entry("9", None) == ("Task 9", "user")
    step = StepState(name="Lint")
    assert verification_started_entry(step) == ("🔍 Verifying: Lint…", "verify")
    assert verification_skipped_entry("no_changes") == (
        "⏩ Verification skipped: no files changed",
        "verify",
    )
    assert verification_finished_entry(step, True) == ("✅ Verification passed: Lint", "verify")
    assert verification_finished_entry(step, False) == ("❌ Verification failed: Lint", "error")


def test_settings_summary_names_the_model_and_new_tasks_tool_access() -> None:
    assert settings_summary(Settings()) == "planner's model: default · new tasks' tools: restricted"
    full = Settings(model="opus", tool_access="full")
    assert (
        settings_summary(full) == "planner's model: opus · new tasks' tools: full (auto-approved)"
    )


def test_model_options_offer_only_the_default_without_choices() -> None:
    assert model_options(()) == ((None,), ["default"])


def test_model_options_put_the_default_before_the_choices() -> None:
    assert model_options(("opus", "sonnet")) == (
        (None, "opus", "sonnet"),
        ["default", "opus", "sonnet"],
    )


def test_tool_access_labels_describe_each_option_in_order() -> None:
    assert tool_access_labels() == [
        "off: no tools",
        "restricted: read-only; anything needing approval is refused",
        "full: auto-approve everything",
    ]


def test_step_lines_time_only_the_running_step() -> None:
    steps = (StepState(name="Lint", status="passed"), StepState(name="Tests", status="running"))
    assert step_lines(steps, "◜ ", 12) == ["✅ Lint", "◜  Tests · 12s"]


def test_every_task_status_has_an_icon_except_running_which_spins() -> None:
    statuses = set(get_args(TaskStatus))
    assert statuses - set(STATUS_ICONS) == {"running"}
    assert STATUS_ICONS["gave_up"] == "🏳"
    assert STATUS_ICONS["draft"] == "📝"


def test_agent_lifecycle_becomes_transcript_entries() -> None:
    started = AgentStarted(agent_id="agent-1", role="coder", task_id="1")
    assert agent_entry(started) == ("▶ agent-1 (coder) started", "agent")
    gave_up = TaskGaveUp(agent_id="agent-1", task_id="1", reason="unclear spec")
    assert agent_entry(gave_up) == ("🏳 agent-1 gave up: unclear spec", "error")
    exits: dict[AgentExitReason, tuple[str, str]] = {
        "finished": ("■ agent-1 exited", "agent"),
        "killed": ("■ agent-1 stopped", "agent"),
        "crashed": ("■ agent-1 crashed", "error"),
    }
    for reason, entry in exits.items():
        assert agent_entry(AgentExited(agent_id="agent-1", reason=reason)) == entry


def test_agent_exchanges_become_transcript_entries() -> None:
    request = AgentRequest(
        agent_id="agent-1", request_id="r", tool="complete_task", arguments={"summary": "ok"}
    )
    assert exchange_entry(request) == ('agent-1 → orbit: complete_task {"summary": "ok"}', "agent")
    reply = AgentReply(agent_id="agent-1", request_id="r", text="Checks passed.")
    assert exchange_entry(reply) == ("orbit → agent-1: Checks passed.", "agent")
    refused = AgentReply(agent_id="agent-1", request_id="r", text="No task.", is_error=True)
    assert exchange_entry(refused) == ("orbit → agent-1: No task.", "error")
    note = ProgressNoted(agent_id="agent-1", task_id="1", note="tests written")
    assert exchange_entry(note) == ("agent-1: tests written", "agent")


def test_no_steps_skip_reason_reads_naturally() -> None:
    text, kind = verification_skipped_entry("no_steps")
    assert text == "⏩ Verification skipped: no verification steps configured"
    assert kind == "verify"


def test_unchanged_skip_reason_reads_naturally() -> None:
    text, kind = verification_skipped_entry("unchanged")
    assert "unchanged" in text and kind == "verify"


def test_agent_output_is_labelled_with_its_agent() -> None:
    said = AssistantMessage(turn_id="t", text="on it", agent_id="agent-2")
    assert output_entry(said) == ("agent-2: on it", "assistant")
    started = ToolCallStarted(
        turn_id="t", call_id="c", name="Read", arguments={}, agent_id="agent-2"
    )
    assert output_entry(started) == ("agent-2: → Read", "tool")
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True, agent_id="a")
    assert output_entry(failed) == ("a: no", "error")


def test_merges_and_notices_become_transcript_entries() -> None:
    merged = TaskMerged(task_id="1", result="merged")
    assert note_entry(merged) == ("⇢ Merged into the base branch.", "verify")
    blocked = TaskMerged(task_id="1", result="blocked", detail="a.txt has local changes")
    assert note_entry(blocked) == ("⚠ Not merged (blocked): a.txt has local changes", "verify")
    assert note_entry(Notice(message="2 stashes")) == ("📌 2 stashes", "verify")


def test_activity_entries_cover_agents_exchanges_and_notices() -> None:
    started = AgentStarted(agent_id="a", role="coder", task_id="1")
    assert activity_entry(started) == agent_entry(started)
    note = ProgressNoted(agent_id="a", task_id="1", note="hi")
    assert activity_entry(note) == exchange_entry(note)
    notice = Notice(message="2 stashes")
    assert activity_entry(notice) == note_entry(notice)


def test_agents_are_tracked_from_their_events() -> None:
    rows: tuple[AgentRow, ...] = ()
    for event in (
        AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
        AgentStarted(agent_id="agent-2", role="reviewer", task_id="2"),
        AgentRequest(agent_id="agent-1", request_id="r", tool="complete_task", arguments={}),
    ):
        rows = track_agents(rows, event)
    assert rows == (
        AgentRow("agent-1", "coder", "1", waiting_on="complete_task"),
        AgentRow("agent-2", "reviewer", "2"),
    )
    rows = track_agents(rows, AgentReply(agent_id="agent-1", request_id="r", text="ok"))
    assert rows[0].waiting_on is None
    rows = track_agents(rows, AgentExited(agent_id="agent-2", reason="finished"))
    assert [r.agent_id for r in rows] == ["agent-1"]
    assert track_agents(rows, Notice(message="x")) == rows


def test_agent_lines_say_what_each_is_doing() -> None:
    assert agent_line(AgentRow("agent-1", "coder", "4"), "2") == "agent-1 coder · task 2 · working"
    waiting = AgentRow("agent-3", "reviewer", "5", waiting_on="complete_task")
    assert agent_line(waiting, "3") == "agent-3 reviewer · task 3 · waiting on complete_task"
    planner = AgentRow("agent-4", "planner", "plan")
    assert agent_line(planner, None) == "agent-4 planner · planning · working"
    # A task that has left the list is named by its id, not taken for planning.
    gone = AgentRow("agent-5", "coder", "7")
    assert agent_line(gone, None) == "agent-5 coder · task 7 · working"


def test_the_review_line_says_whether_review_is_paused() -> None:
    assert review_line(None) == ""
    assert review_line(ReviewStatus(paused=False, open_batches=0)) == "review: active"
    assert review_line(ReviewStatus(paused=False, open_batches=2)) == "review: active · 2 open"
    assert review_line(ReviewStatus(paused=True, open_batches=0)) == "review: paused"


def test_each_started_agent_in_another_role_gets_a_tab_until_it_exits() -> None:
    tabs: tuple[ConversationTab, ...] = ()
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-1", role="checker", task_id="1"))
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-2", role="linter", task_id="2"))
    assert tabs == (ConversationTab("agent-1", "checker"), ConversationTab("agent-2", "linter"))
    tabs = track_tabs(tabs, AgentExited(agent_id="agent-1", reason="finished"))
    assert tabs == (ConversationTab("agent-2", "linter"),)
    assert track_tabs(tabs, Notice(message="x")) is tabs


def test_tabs_are_named_by_role_and_numbered_among_those_open() -> None:
    assert ALL_TAB_LABEL == "All"
    assert tab_labels(()) == []
    tabs: tuple[ConversationTab, ...] = (
        ConversationTab("agent-1", "coder"),
        ConversationTab("agent-2", "reviewer"),
        ConversationTab("agent-3", "coder"),
        ConversationTab("agent-4", "coder"),
    )
    assert tab_labels(tabs) == ["coder", "reviewer", "coder 2", "coder 3"]
    tabs = track_tabs(tabs, AgentExited(agent_id="agent-1", reason="finished"))
    assert tab_labels(tabs) == ["reviewer", "coder", "coder 2"]


def test_output_from_a_background_agent_marks_its_tab_unread() -> None:
    tabs = (ConversationTab("agent-1", "coder"), ConversationTab("agent-2", "coder"))
    outputs: list[Event] = [
        AssistantMessage(turn_id="t", text="hi", agent_id="agent-2"),
        ToolCallStarted(turn_id="t", call_id="c", name="Read", arguments={}, agent_id="agent-2"),
        ToolCallFinished(turn_id="t", call_id="c", output="ok", agent_id="agent-2"),
        AgentRequest(agent_id="agent-2", request_id="r", tool="get_task", arguments={}),
        AgentReply(agent_id="agent-2", request_id="r", text="ok"),
        ProgressNoted(agent_id="agent-2", task_id="1", note="n"),
        TaskGaveUp(agent_id="agent-2", task_id="1", reason="r"),
        ErrorOccurred(agent_id="agent-2", message="boom"),
        TurnFinished(turn_id="t", agent_id="agent-2", reason="cancelled"),
    ]
    for event in outputs:
        marked = track_tabs(tabs, event, active="agent-1")
        assert marked == (tabs[0], ConversationTab("agent-2", "coder", True))
        assert tab_labels(marked) == ["coder", "coder 2 *"]
        # Output for the tab being looked at isn't unread.
        assert track_tabs(tabs, event, active="agent-2") == tabs


def test_events_with_no_agent_mark_nothing() -> None:
    tabs = (ConversationTab("agent-1", "coder"),)
    assert track_tabs(tabs, AssistantMessage(turn_id="t", text="hi")) is tabs
    assert track_tabs(tabs, Notice(message="x"), active=None) is tabs


def test_a_turn_ending_as_expected_marks_nothing() -> None:
    tabs = (ConversationTab("agent-1", "coder"),)
    for reason in ("completed", "error"):
        event = TurnFinished(turn_id="t", agent_id="agent-1", reason=reason)
        assert track_tabs(tabs, event) is tabs


def test_marking_a_tab_read_clears_only_its_marker() -> None:
    tabs = (ConversationTab("agent-1", "coder", True), ConversationTab("agent-2", "coder", True))
    assert mark_tab_read(tabs, "agent-1") == (
        ConversationTab("agent-1", "coder"),
        ConversationTab("agent-2", "coder", True),
    )
    assert tab_labels(mark_tab_read(tabs, "agent-1")) == ["coder", "coder 2 *"]


def test_the_pinned_tabs_are_exactly_the_backends_built_in_roles() -> None:
    assert set(ROLE_TABS) == set(BUILT_IN_ROLE_NAMES)
    assert len(ROLE_TABS) == len(BUILT_IN_ROLE_NAMES)


def test_the_pinned_tabs_are_the_built_in_roles_in_order() -> None:
    assert ROLE_TABS == ("planner", "coder", "reviewer", "super-reviewer", "configurator")
    assert PLANNER == "planner"
    assert [tab.key for tab in PINNED_TABS] == list(ROLE_TABS)
    assert tab_labels(PINNED_TABS) == [
        "planner",
        "coder",
        "reviewer",
        "super-reviewer",
        "configurator",
    ]


def test_agents_in_a_pinned_role_get_no_tab_of_their_own() -> None:
    for index, role in enumerate(ROLE_TABS):
        started = AgentStarted(agent_id=f"agent-{index}", role=role, task_id="1")
        assert [tab.key for tab in track_tabs(PINNED_TABS, started)] == list(ROLE_TABS)


def test_a_pinned_roles_agent_exiting_leaves_its_tab() -> None:
    started = AgentStarted(agent_id="agent-1", role="coder", task_id="1")
    roles = track_roles({}, started)
    exited = AgentExited(agent_id="agent-1", reason="finished")
    assert track_roles(roles, exited) is roles
    assert track_tabs(track_tabs(PINNED_TABS, started), exited, roles=roles) == PINNED_TABS


def test_an_agent_named_like_a_pinned_role_doesnt_close_its_tab() -> None:
    exited = AgentExited(agent_id="coder", reason="finished")
    assert track_tabs(PINNED_TABS, exited) == PINNED_TABS


def test_each_pinned_roles_agents_are_keyed_to_its_tab() -> None:
    roles: dict[str, str] = {}
    for index, role in enumerate(ROLE_TABS):
        started = AgentStarted(agent_id=f"agent-{index}", role=role, task_id="1")
        roles = dict(track_roles(roles, started))
    assert [tab_key(f"agent-{index}", roles) for index in range(len(ROLE_TABS))] == list(ROLE_TABS)
    assert tab_key("other", roles) == "other"
    assert tab_key(None, roles) is None


def test_concurrent_reviewers_share_a_tab_and_the_super_reviewer_has_its_own() -> None:
    roles: Mapping[str, str] = {}
    for agent, role in (("a1", "reviewer"), ("a2", "reviewer"), ("a3", "super-reviewer")):
        roles = track_roles(roles, AgentStarted(agent_id=agent, role=role, task_id="1"))
    assert tab_key("a1", roles) == tab_key("a2", roles) == "reviewer"
    assert tab_key("a3", roles) == "super-reviewer"


def test_other_roles_are_not_tracked() -> None:
    roles: Mapping[str, str] = {}
    checker = AgentStarted(agent_id="startup-check", role="checker", task_id="")
    assert track_roles(roles, checker) is roles
    assert track_roles(roles, Notice(message="x")) is roles


def test_a_checker_gets_a_temporary_tab_after_the_pinned_ones() -> None:
    checker = AgentStarted(agent_id="startup-check", role="checker", task_id="")
    tabs = track_tabs(PINNED_TABS, checker)
    assert tabs == (*PINNED_TABS, ConversationTab("startup-check", "checker"))
    assert tab_labels(tabs)[-1] == "checker"
    exited = AgentExited(agent_id="startup-check", reason="finished")
    assert track_tabs(tabs, exited) == PINNED_TABS


def test_pinned_role_output_marks_its_tab_unless_shown() -> None:
    roles = {"agent-1": "reviewer", "agent-2": "reviewer"}
    said = AssistantMessage(turn_id="t", text="hi", agent_id="agent-2")
    marked = track_tabs(PINNED_TABS, said, active=None, roles=roles)
    assert tab_labels(marked) == [
        "planner",
        "coder",
        "reviewer *",
        "super-reviewer",
        "configurator",
    ]
    assert track_tabs(PINNED_TABS, said, active="reviewer", roles=roles) == PINNED_TABS
    assert mark_tab_read(marked, "reviewer") == PINNED_TABS


def test_entries_are_labelled_by_their_agent_when_they_have_one() -> None:
    assert agent_labelled(("boom", "error"), "agent-1") == ("agent-1: boom", "error")
    assert agent_labelled(("boom", "error"), None) == ("boom", "error")
    end = turn_end_entry("cancelled")
    assert end is not None
    assert agent_labelled(end, "agent-2") == ("agent-2: [turn ended: cancelled]", "tool")
    assert agent_labelled(end, None) == end


def _pinned(agent: str, role: str) -> tuple[AgentStarted, AgentExited]:
    return (
        AgentStarted(agent_id=agent, role=role, task_id="1"),
        AgentExited(agent_id=agent, reason="finished"),
    )


def test_a_pinned_roles_tab_is_marked_running_from_its_agents_start_to_its_exit() -> None:
    started, exited = _pinned("agent-1", "coder")
    roles = track_roles({}, started)
    tabs = track_tabs(PINNED_TABS, started)
    assert tabs == tuple(
        replace(tab, running=1) if tab.key == "coder" else tab for tab in PINNED_TABS
    )
    assert tab_labels(tabs) == ["planner", "● coder", "reviewer", "super-reviewer", "configurator"]
    assert track_tabs(tabs, exited, roles=roles) == PINNED_TABS


def test_a_roles_marker_stays_until_its_last_agent_exits() -> None:
    started_1, exited_1 = _pinned("agent-1", "reviewer")
    started_2, exited_2 = _pinned("agent-2", "reviewer")
    roles: Mapping[str, str] = {}
    tabs = PINNED_TABS
    for started in (started_1, started_2):
        roles = track_roles(roles, started)
        tabs = track_tabs(tabs, started, roles=roles)
    assert tab_labels(tabs)[2] == "● reviewer"
    tabs = track_tabs(tabs, exited_1, roles=roles)
    assert tab_labels(tabs)[2] == "● reviewer"
    tabs = track_tabs(tabs, exited_2, roles=roles)
    assert tab_labels(tabs)[2] == "reviewer"
    assert tabs == PINNED_TABS


def test_the_planner_agents_start_and_exit_dont_mark_its_tab() -> None:
    started, exited = _pinned("planner-agent", "planner")
    roles = track_roles({}, started)
    assert track_tabs(PINNED_TABS, started) == PINNED_TABS
    assert track_tabs(PINNED_TABS, exited, roles=roles) == PINNED_TABS


def test_the_planner_tab_is_marked_while_the_planner_is_busy() -> None:
    busy = mark_planner_busy(PINNED_TABS, True)
    assert busy == (replace(PINNED_TABS[0], running=1), *PINNED_TABS[1:])
    assert tab_labels(busy)[0] == "● planner"
    assert mark_planner_busy(busy, True) == busy
    assert mark_planner_busy(busy, False) == PINNED_TABS


def test_the_running_marker_leads_and_the_unread_marker_trails() -> None:
    tabs = (ConversationTab("coder", "coder", unread=True, running=1),)
    assert tab_labels(tabs) == ["● coder *"]
    assert tab_labels(mark_tab_read(tabs, "coder")) == ["● coder"]
    assert tab_labels(mark_planner_busy((PINNED_TABS[0],), True)) == ["● planner"]
    unread = track_tabs(tabs, AgentStarted(agent_id="a", role="coder", task_id="1"))
    assert tab_labels(unread) == ["● coder *"]


def test_a_tasks_notes_are_keyed_to_its_roles_pinned_tab() -> None:
    for role in ROLE_TABS:
        assert task_tab_key(TaskState(id="1", task=TaskSpec(prompt="p", role=role))) == role


def test_a_task_in_a_role_with_no_pinned_tab_has_no_tab_key() -> None:
    assert task_tab_key(TaskState(id="1", task=TaskSpec(prompt="p", role="checker"))) is None


def test_an_unknown_task_has_no_tab_key() -> None:
    assert task_tab_key(None) is None


def test_marking_a_tab_unread_skips_the_shown_tab_and_all() -> None:
    marked = mark_tab_unread(PINNED_TABS, "coder", active="reviewer")
    assert marked == tuple(
        replace(tab, unread=True) if tab.key == "coder" else tab for tab in PINNED_TABS
    )
    assert mark_tab_unread(PINNED_TABS, "coder", active="coder") == PINNED_TABS
    assert mark_tab_unread(PINNED_TABS, None, active=None) == PINNED_TABS


def test_only_tool_entries_start_collapsed() -> None:
    assert starts_collapsed("tool")
    assert not any(
        starts_collapsed(kind) for kind in ("user", "assistant", "verify", "agent", "error")
    )


def test_an_entry_is_collapsible_when_it_has_more_than_one_line() -> None:
    assert collapsible("one\ntwo") and collapsible("one\n")
    assert not collapsible("one")


def test_entry_labels_mark_collapsible_entries_and_count_collapsed_lines() -> None:
    assert entry_label("one", collapsed=False) == "one"
    assert entry_label("one", collapsed=True) == "one"
    assert entry_label("one\ntwo", collapsed=False) == "▾ one\ntwo"
    assert entry_label("one\ntwo", collapsed=True) == "▸ one (+1 line)"
    assert entry_label("one\ntwo\nthree", collapsed=True) == "▸ one (+2 lines)"


def test_gutter_symbols_differ_and_fill_one_column() -> None:
    assert GUTTER_BAR != GUTTER_MARKER
    assert len(GUTTER_BAR) == len(GUTTER_MARKER) == 1


def test_gutter_is_a_bar_for_an_entry_the_cursor_isnt_on() -> None:
    assert gutter(marked=False) == f"{GUTTER_BAR} "


def test_gutter_is_the_marker_for_the_cursors_entry() -> None:
    assert gutter(marked=True) == f"{GUTTER_MARKER} "


def test_gutter_is_two_columns_whether_marked_or_not() -> None:
    assert len(gutter(marked=False)) == len(gutter(marked=True)) == 2


def test_planner_prompt_splits_into_title_and_body_and_joins_back() -> None:
    prompt = "Title\n\nBody line 1\nBody line 2"
    assert split_prompt(prompt) == ("Title", "Body line 1\nBody line 2")
    assert join_prompt(*split_prompt(prompt)) == prompt


def test_fix_prompt_round_trips_through_split_and_join() -> None:
    prompt = "Fix the failed verification step: pytest\n\n$ uv run pytest\n...\nMore"
    assert join_prompt(*split_prompt(prompt)) == prompt


def test_single_line_prompt_has_no_body() -> None:
    assert split_prompt("Do the thing") == ("Do the thing", "")
    assert join_prompt(*split_prompt("Do the thing")) == "Do the thing"


def test_empty_prompt_splits_to_nothing() -> None:
    assert split_prompt("") == ("", "")


def test_body_on_the_second_line_joins_back_after_a_blank_line() -> None:
    assert split_prompt("a\nb") == ("a", "b")
    assert join_prompt("a", "b") == "a\n\nb"


def test_blank_lines_and_trailing_whitespace_around_the_body_are_dropped() -> None:
    assert split_prompt("Title\n  \n\n  body\n\n  ") == ("Title", "  body")
    assert join_prompt("Title", "\n  body  \n\n") == "Title\n\n  body"


def test_body_keeps_its_leading_indentation_through_split_and_join() -> None:
    prompt = "Title\n\n    code = 1\nmore\n"
    assert split_prompt(prompt) == ("Title", "    code = 1\nmore")
    assert join_prompt(*split_prompt(prompt)) == "Title\n\n    code = 1\nmore"


def test_joining_a_blank_body_gives_just_the_title() -> None:
    assert join_prompt("Title", "") == "Title"
    assert join_prompt("Title", "  \n ") == "Title"


def test_joining_strips_the_title() -> None:
    assert join_prompt("  Title \n", "body") == "Title\n\nbody"


def test_interior_blank_lines_of_the_body_survive_split_and_join() -> None:
    prompt = "Title\n\na\n\n  \nb"
    assert split_prompt(prompt) == ("Title", "a\n\n  \nb")
    assert join_prompt(*split_prompt(prompt)) == prompt


def test_trailing_whitespace_on_an_interior_body_line_survives() -> None:
    assert split_prompt("Title\n\na  \nb") == ("Title", "a  \nb")


def test_only_newlines_delimit_body_lines() -> None:
    # str.splitlines would also split on these; the editor must hand the body back unchanged.
    for prompt in ("Title\n\na\u2028b", "Title\n\na\x0cb", "Title\n\na\r\nb"):
        assert join_prompt(*split_prompt(prompt)) == prompt


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


def tasks_changed(*task_ids: str) -> TasksChanged:
    return TasksChanged(tasks=tuple(TaskState(id=i, task=TaskSpec(prompt=i)) for i in task_ids))


def steps_changed(*names: str) -> VerificationChanged:
    return VerificationChanged(steps=tuple(StepState(name=name) for name in names))


def test_coalesce_collapses_consecutive_task_lists_to_the_last() -> None:
    last = tasks_changed("1", "2", "3")
    assert coalesce([tasks_changed("1"), tasks_changed("1", "2"), last]) == [last]


def test_coalesce_keeps_a_task_list_a_task_start_reads() -> None:
    events: list[Event] = [tasks_changed("1"), TaskStarted(task_id="1"), tasks_changed("1", "2")]
    assert coalesce(events) == events


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


@pytest.mark.parametrize(
    ("name", "arguments", "summary"),
    [
        ("Read", {"file_path": "src/a.py"}, "Read src/a.py"),
        ("Write", {"file_path": "src/a.py", "content": "x"}, "Write src/a.py"),
        ("Edit", {"file_path": "src/a.py", "old_string": "x"}, "Edit src/a.py"),
        ("MultiEdit", {"file_path": "src/a.py", "edits": []}, "MultiEdit src/a.py"),
        ("NotebookEdit", {"notebook_path": "n.ipynb"}, "NotebookEdit n.ipynb"),
        ("Bash", {"command": "uv run pytest"}, "Bash uv run pytest"),
        ("Grep", {"pattern": "def x"}, "Grep def x"),
        ("Grep", {"pattern": "def x", "path": "src"}, "Grep def x in src"),
        ("Glob", {"pattern": "*.py"}, "Glob *.py"),
        ("Glob", {"pattern": "*.py", "path": "tests"}, "Glob *.py in tests"),
        ("WebSearch", {"query": "textual"}, "WebSearch textual"),
        ("WebFetch", {"url": "https://x.org"}, "WebFetch https://x.org"),
        ("Task", {"description": "Find it", "prompt": "…"}, "Task Find it"),
        ("Agent", {"description": "Find it"}, "Agent Find it"),
        ("TodoWrite", {"todos": [{}, {}, {}]}, "TodoWrite 3 todos"),
    ],
)
def test_tool_call_summary_names_a_built_in_tools_key_argument(
    name: str, arguments: dict[str, Any], summary: str
) -> None:
    assert tool_call_summary(name, arguments) == summary


@pytest.mark.parametrize("field", ["summary", "title", "note", "reason", "name"])
def test_tool_call_summary_shows_an_orbit_tools_first_text_field(field: str) -> None:
    arguments = {field: "Fix it", "zzz": "other"}
    assert tool_call_summary("mcp__orbit__complete_task", arguments) == "complete_task Fix it"


def test_tool_call_summary_prefers_an_orbit_tools_earlier_field() -> None:
    arguments = {"name": "later", "summary": "first"}
    assert tool_call_summary("mcp__orbit__x", arguments) == "x first"


def test_tool_call_summary_shows_an_orbit_tool_without_a_text_field_by_name() -> None:
    assert tool_call_summary("mcp__orbit__get_task", {}) == "get_task"
    assert tool_call_summary("mcp__orbit__run_check", {"step": "lint"}) == "run_check"


def test_tool_call_summary_shows_an_unknown_tools_arguments_as_json() -> None:
    summary = tool_call_summary("upper", {"text": "hé"})
    assert summary == 'upper {"text": "hé"}'


def test_tool_call_summary_falls_back_to_json_without_its_key_argument() -> None:
    assert tool_call_summary("Read", {}) == "Read {}"
    assert tool_call_summary("Bash", {"command": 3}) == 'Bash {"command": 3}'
    assert tool_call_summary("TodoWrite", {"todos": "x"}) == 'TodoWrite {"todos": "x"}'
    assert tool_call_summary("Grep", {"pattern": "x", "path": 1}) == "Grep x"


def test_tool_call_summary_shows_a_commands_first_line() -> None:
    arguments = {"command": "cd  src;\techo hi\nls -la\npwd"}
    assert tool_call_summary("Bash", arguments) == "Bash cd src; echo hi"


def test_tool_call_summary_collapses_whitespace_in_its_key_argument() -> None:
    arguments = {"summary": "Add  a\nthing"}
    assert tool_call_summary("mcp__orbit__complete_task", arguments) == "complete_task Add a thing"


def test_tool_call_summary_keeps_120_characters() -> None:
    path = "a" * (120 - len("Read "))
    assert tool_call_summary("Read", {"file_path": path}) == f"Read {path}"


def test_tool_call_summary_cuts_121_characters_with_an_ellipsis() -> None:
    path = "a" * (121 - len("Read "))
    summary = tool_call_summary("Read", {"file_path": path})
    assert len(summary) == 120
    assert summary == f"Read {path[:114]}…"


def test_a_tool_call_entry_collapses_its_full_arguments_beneath_a_summary() -> None:
    arguments = {"file_path": "src/foo.py", "content": "a\nb"}
    started = ToolCallStarted(turn_id="t", call_id="c", name="Write", arguments=arguments)
    text, kind = output_entry(started)
    first, _, rest = text.partition("\n")
    assert first == "→ Write src/foo.py"
    assert json.loads(rest) == arguments
    assert starts_collapsed(kind)
    assert entry_label(text, collapsed=True) == "▸ → Write src/foo.py (+4 lines)"


def test_a_tool_call_entry_without_arguments_is_one_line() -> None:
    started = ToolCallStarted(turn_id="t", call_id="c", name="mcp__orbit__get_task", arguments={})
    text, _ = output_entry(started)
    assert text == "→ mcp__orbit__get_task"
    assert not collapsible(text)


def test_a_tool_call_entrys_arguments_keep_non_ascii_text() -> None:
    started = ToolCallStarted(turn_id="t", call_id="c", name="x", arguments={"a": "é"})
    assert output_entry(started)[0] == '→ x {"a": "é"}\n{\n  "a": "é"\n}'
