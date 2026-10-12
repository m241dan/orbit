from orbit.protocol import (
    AssistantMessage,
    Settings,
    StepState,
    TaskSpec,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
)
from orbit.tui.view import (
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
