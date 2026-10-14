from typing import get_args

from orbit.protocol import (
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ProgressNoted,
    Settings,
    StepState,
    TaskGaveUp,
    TaskSpec,
    TaskState,
    TaskStatus,
    ToolCallFinished,
    ToolCallStarted,
)
from orbit.tui.view import (
    STATUS_ICONS,
    agent_entry,
    exchange_entry,
    output_entry,
    settings_summary,
    step_lines,
    task_started_entry,
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
