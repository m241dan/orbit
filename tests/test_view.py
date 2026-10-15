from collections.abc import Mapping
from dataclasses import replace
from typing import get_args

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
    StepState,
    TaskGaveUp,
    TaskMerged,
    TaskSpec,
    TaskState,
    TaskStatus,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from orbit.tui.view import (
    ALL_TAB_LABEL,
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
    exchange_entry,
    mark_planner_busy,
    mark_tab_read,
    mark_tab_unread,
    note_entry,
    output_entry,
    review_line,
    settings_summary,
    step_lines,
    tab_key,
    tab_labels,
    task_started_entry,
    task_tab_key,
    track_agents,
    track_roles,
    track_tabs,
    turn_end_entry,
    verification_finished_entry,
    verification_skipped_entry,
    verification_started_entry,
)


def test_turn_output_becomes_transcript_entries() -> None:
    assert output_entry(AssistantMessage(turn_id="t", text="hi")) == ("hi", "assistant")
    started = ToolCallStarted(turn_id="t", call_id="c", name="Read", arguments={"path": "a"})
    assert output_entry(started) == ('→ Read {"path": "a"}', "tool")
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
    assert settings_summary(Settings()) == "model: default · new tasks' tools: restricted"
    full = Settings(model="opus", tool_access="full")
    assert settings_summary(full) == "model: opus · new tasks' tools: full (auto-approved)"


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
    assert output_entry(started) == ("agent-2: → Read {}", "tool")
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True, agent_id="a")
    assert output_entry(failed) == ("a: no", "error")


def test_merges_and_notices_become_transcript_entries() -> None:
    merged = TaskMerged(task_id="1", result="merged")
    assert note_entry(merged) == ("⇢ Merged into the base branch.", "verify")
    blocked = TaskMerged(task_id="1", result="blocked", detail="a.txt has local changes")
    assert note_entry(blocked) == ("⚠ Not merged (blocked): a.txt has local changes", "verify")
    assert note_entry(Notice(message="2 stashes")) == ("📌 2 stashes", "verify")


def test_activity_entries_cover_agents_exchanges_merges_and_notices() -> None:
    started = AgentStarted(agent_id="a", role="coder", task_id="1")
    assert activity_entry(started) == agent_entry(started)
    note = ProgressNoted(agent_id="a", task_id="1", note="hi")
    assert activity_entry(note) == exchange_entry(note)
    merged = TaskMerged(task_id="1", result="merged")
    assert activity_entry(merged) == note_entry(merged)


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
