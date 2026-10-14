from typing import get_args

from orbit.protocol import (
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
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
)
from orbit.tui.view import (
    ALL_TAB_LABEL,
    STATUS_ICONS,
    AgentRow,
    ConversationTab,
    activity_entry,
    agent_entry,
    agent_line,
    exchange_entry,
    next_filter,
    note_entry,
    output_entry,
    review_line,
    settings_summary,
    step_lines,
    tab_labels,
    task_started_entry,
    track_agents,
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
    step = StepState(name="Lint")
    assert verification_started_entry(step) == ("🔍 Verifying: Lint…", "verify")
    assert verification_skipped_entry("no_changes") == (
        "⏩ Verification skipped: no files changed",
        "verify",
    )
    assert verification_finished_entry(step, True) == ("✅ Verification passed: Lint", "verify")
    assert verification_finished_entry(step, False) == ("❌ Verification failed: Lint", "error")


def test_settings_summary_names_the_model_and_tool_access() -> None:
    assert settings_summary(Settings()) == "model: default · tools: restricted"
    full = Settings(model="opus", tool_access="full")
    assert settings_summary(full) == "model: opus · tools: full (auto-approved)"


def test_step_lines_time_only_the_running_step() -> None:
    steps = (StepState(name="Lint", status="passed"), StepState(name="Tests", status="running"))
    assert step_lines(steps, "◜ ", 12) == ["✅ Lint", "◜  Tests · 12s"]


def test_every_task_status_has_an_icon_except_running_which_spins() -> None:
    statuses = {status for part in get_args(TaskStatus) for status in get_args(part)}
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


def test_the_review_line_says_whether_review_is_paused() -> None:
    assert review_line(None) == ""
    assert review_line(ReviewStatus(paused=False, open_batches=0)) == "review: active"
    assert review_line(ReviewStatus(paused=False, open_batches=2)) == "review: active · 2 open"
    assert review_line(ReviewStatus(paused=True, open_batches=0)) == "review: paused"


def test_the_filter_cycles_through_everything_and_each_agent() -> None:
    seen = ("agent-1", "agent-2")
    assert next_filter(None, seen) == "agent-1"
    assert next_filter("agent-1", seen) == "agent-2"
    assert next_filter("agent-2", seen) is None
    assert next_filter(None, ()) is None
    assert next_filter("agent-9", seen) is None


def test_each_started_agent_gets_a_tab_until_it_exits() -> None:
    tabs: tuple[ConversationTab, ...] = ()
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-1", role="coder", task_id="1"))
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-2", role="reviewer", task_id="2"))
    assert tabs == (ConversationTab("agent-1", "coder"), ConversationTab("agent-2", "reviewer"))
    tabs = track_tabs(tabs, AgentExited(agent_id="agent-1", reason="finished"))
    assert tabs == (ConversationTab("agent-2", "reviewer"),)
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
