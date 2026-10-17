import json

import pytest
from rich.console import Console
from rich.style import Style

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
    Notice,
    ProgressNoted,
    StepState,
    TaskGaveUp,
    TaskMerged,
    TaskSpec,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    Usage,
)
from orbit.tui.view import (
    GUTTER_BAR,
    GUTTER_MARKER,
    PROMPT_LINES_SHOWN,
    PROMPT_MARKER,
    SPINNER_FPS,
    DrawnRule,
    EntryKind,
    ExitRule,
    ToolCall,
    activity_entry,
    agent_entry,
    agent_labelled,
    banded,
    collapsible,
    conversation_title,
    duration_text,
    entry_label,
    entry_markdown,
    entry_prefix,
    error_entry,
    exchange_entry,
    exit_rule,
    final_answer,
    gutter,
    hanging_indent,
    note_entry,
    output_entry,
    ruled,
    shows_markdown,
    speaker_label,
    starts_collapsed,
    task_started_entry,
    token_text,
    tool_call_entry,
    tool_call_finished,
    tool_call_started,
    turn_rule_entry,
    usage_text,
    verification_finished_entry,
    verification_skipped_entry,
    verification_started_entry,
)


def test_turn_output_becomes_transcript_entries() -> None:
    assert output_entry(AssistantMessage(turn_id="t", text="hi")) == ("hi", "assistant")
    assert output_entry(ToolCallFinished(turn_id="t", call_id="c", output="ok")) == ("ok", "tool")
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True)
    assert output_entry(failed) == ("no", "error")


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


def test_agent_lifecycle_becomes_transcript_entries() -> None:
    started = AgentStarted(agent_id="agent-1", role="coder", task_id="1")
    assert agent_entry(started) == ("▶ agent-1 (coder) started", "agent")
    gave_up = TaskGaveUp(agent_id="agent-1", task_id="1", reason="unclear spec")
    assert agent_entry(gave_up) == ("🏳 agent-1 gave up: unclear spec", "error")


def test_agent_exchanges_become_transcript_entries() -> None:
    reply = AgentReply(agent_id="agent-1", request_id="r", text="Checks passed.")
    assert exchange_entry(reply) == ("orbit → agent-1: Checks passed.", "agent")
    refused = AgentReply(agent_id="agent-1", request_id="r", text="No task.", is_error=True)
    assert exchange_entry(refused) == ("orbit → agent-1: No task.", "error")
    note = ProgressNoted(agent_id="agent-1", task_id="1", note="tests written")
    assert exchange_entry(note) == ("agent-1: tests written", "agent")


def test_an_orbit_request_collapses_its_full_arguments_beneath_a_summary() -> None:
    arguments = {"title": "Add a flag", "prompt": "First paragraph.\n\nSecond paragraph."}
    request = AgentRequest(
        agent_id="agent-1", request_id="r", tool="suggest_task", arguments=arguments
    )
    text, kind = exchange_entry(request)
    first, _, rest = text.partition("\n")
    assert first == "agent-1 → orbit: suggest_task Add a flag"
    assert json.loads(rest) == arguments
    assert starts_collapsed(kind, text)


def test_an_orbit_request_summary_is_clipped_to_the_tool_call_limit() -> None:
    title = "a" * 200
    request = AgentRequest(
        agent_id="agent-1", request_id="r", tool="suggest_task", arguments={"title": title}
    )
    first = exchange_entry(request)[0].partition("\n")[0]
    assert first == f"agent-1 → orbit: suggest_task {title[:106]}…"


def test_an_orbit_request_without_arguments_is_one_line() -> None:
    request = AgentRequest(agent_id="agent-1", request_id="r", tool="get_task", arguments={})
    text, _ = exchange_entry(request)
    assert text == "agent-1 → orbit: get_task"
    assert not collapsible(text)


def test_an_orbit_request_keeps_non_ascii_arguments() -> None:
    request = AgentRequest(
        agent_id="agent-1", request_id="r", tool="complete_task", arguments={"summary": "café ✓"}
    )
    text, _ = exchange_entry(request)
    assert text.partition("\n")[0] == "agent-1 → orbit: complete_task café ✓"
    assert '"café ✓"' in text


def test_no_steps_skip_reason_reads_naturally() -> None:
    text, kind = verification_skipped_entry("no_steps")
    assert text == "⏩ Verification skipped: no verification steps configured"
    assert kind == "verify"


def test_unchanged_skip_reason_reads_naturally() -> None:
    text, kind = verification_skipped_entry("unchanged")
    assert "unchanged" in text and kind == "verify"


def test_agent_tool_output_and_errors_are_labelled_with_their_agent() -> None:
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True, agent_id="a")
    assert output_entry(failed) == ("a: no", "error")


def test_an_agents_message_leaves_its_agent_out_of_its_text() -> None:
    said = AssistantMessage(turn_id="t", text="on it", agent_id="agent-2")
    assert output_entry(said) == ("on it", "assistant")


def test_an_agents_message_is_labelled_when_the_speaker_changes() -> None:
    assert speaker_label("assistant", "agent-2", None) == "agent-2"
    assert speaker_label("assistant", "agent-2", "agent-1") == "agent-2"
    assert speaker_label("assistant", "agent-2", PLANNER) == "agent-2"


def test_an_agents_message_after_its_own_entry_is_not_labelled() -> None:
    assert speaker_label("assistant", "agent-2", "agent-2") is None


def test_the_planners_messages_are_never_labelled() -> None:
    assert speaker_label("assistant", None, "agent-1") is None
    assert speaker_label("assistant", PLANNER, "agent-1") is None


def test_entries_other_than_messages_are_never_labelled() -> None:
    assert speaker_label("tool", "agent-2", "agent-1") is None


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


def test_entries_are_labelled_by_their_agent_when_they_have_one() -> None:
    assert agent_labelled(("boom", "error"), "agent-1") == ("agent-1: boom", "error")
    assert agent_labelled(("boom", "error"), None) == ("boom", "error")


def test_only_tool_and_request_entries_start_collapsed() -> None:
    assert starts_collapsed("tool", "a\nb") and starts_collapsed("request", "a\nb")
    assert not any(
        starts_collapsed(kind, "a\nb") for kind in ("user", "assistant", "verify", "agent", "error")
    )


def test_an_entry_of_one_line_never_starts_collapsed() -> None:
    assert not starts_collapsed("tool", "a")


def test_a_prompt_starts_collapsed_only_past_its_lines_shown() -> None:
    lines = [f"line {i}" for i in range(PROMPT_LINES_SHOWN + 1)]
    assert starts_collapsed("prompt", "\n".join(lines))
    assert not starts_collapsed("prompt", "\n".join(lines[:-1]))


def test_an_entry_is_collapsible_when_it_has_more_than_one_line() -> None:
    assert collapsible("one\ntwo") and collapsible("one\n")
    assert not collapsible("one")


def test_entry_labels_mark_collapsible_entries_and_count_collapsed_lines() -> None:
    assert entry_label("tool", "one", collapsed=False) == "one"
    assert entry_label("tool", "one", collapsed=True) == "one"
    assert entry_label("tool", "one\ntwo", collapsed=False) == "▾ one\ntwo"
    assert entry_label("tool", "one\ntwo", collapsed=True) == "▸ one (+1 line)"
    assert entry_label("tool", "one\ntwo\nthree", collapsed=True) == "▸ one (+2 lines)"


def test_a_prompts_label_puts_the_prompt_marker_ahead_of_the_collapse_marker() -> None:
    assert entry_label("prompt", "one", collapsed=False) == "❯ one"
    assert entry_label("prompt", "one\ntwo", collapsed=False) == "❯ ▾ one\ntwo"
    assert entry_label("prompt", "one\ntwo", collapsed=True) == "❯ ▸ one (+1 line)"


def test_only_prompts_have_a_prefix() -> None:
    assert entry_prefix("prompt") == PROMPT_MARKER
    assert not any(entry_prefix(kind) for kind in ("user", "assistant", "tool", "error"))


def test_only_prompts_are_banded() -> None:
    assert banded("prompt")
    assert not any(banded(kind) for kind in ("user", "assistant", "tool", "error"))


def test_only_expanded_assistant_and_prompt_entries_show_markdown() -> None:
    assert shows_markdown("assistant", collapsed=False)
    assert not shows_markdown("assistant", collapsed=True)
    assert shows_markdown("prompt", collapsed=False)
    assert not shows_markdown("prompt", collapsed=True)
    assert not any(
        shows_markdown(kind, collapsed=False)
        for kind in ("user", "tool", "verify", "agent", "request", "error")
    )


def test_a_collapsible_entry_hangs_its_marker_beside_its_markdown() -> None:
    assert hanging_indent("assistant", "one\ntwo") == ("▾ ", "  ")


def test_an_entry_of_one_line_has_no_hanging_indent() -> None:
    assert hanging_indent("assistant", "one") == ("", "")


def test_a_prompt_hangs_its_marker_and_any_collapse_marker_beside_its_markdown() -> None:
    assert hanging_indent("prompt", "one") == ("❯ ", "  ")
    assert hanging_indent("prompt", "one\ntwo") == ("❯ ▾ ", "    ")


def test_prompts_are_never_labelled() -> None:
    assert speaker_label("prompt", "agent-2", "agent-1") is None


def markdown_rows(text: str, width: int = 40) -> list[str]:
    """``text``'s markdown as rendered ``width`` columns wide, a string per row."""
    console = Console(width=width)
    markdown = entry_markdown(text, Style())
    lines = console.render_lines(markdown, console.options, pad=False)
    return ["".join(segment.text for segment in line).rstrip() for line in lines]


def test_markdown_headings_are_left_aligned_without_a_box() -> None:
    rows = markdown_rows("# Title\n\n## Section\n\ntext")
    assert rows == ["Title", "", "Section", "", "text"]


def test_markdown_headings_are_bold_and_only_h1_underlined() -> None:
    console = Console(width=40)
    for text, underline in (("# Title", True), ("## Section", False)):
        lines = console.render_lines(entry_markdown(text, Style()), console.options)
        style = next(segment.style for segment in lines[0] if segment.text.strip())
        assert style is not None and style.bold and bool(style.underline) is underline


def test_markdown_shows_emphasis_code_and_lists_without_their_syntax() -> None:
    assert markdown_rows("**bold** and `code`\n\n- item") == ["bold and code", "", " • item"]


def test_markdown_links_show_their_url_after_their_text() -> None:
    assert markdown_rows("see [docs](https://example.com)") == ["see docs (https://example.com)"]


def test_markdown_takes_the_entrys_style_as_its_base() -> None:
    console = Console(width=40)
    style = Style(color="red")
    lines = console.render_lines(entry_markdown("plain", style), console.options)
    assert lines[0][0].style is not None and lines[0][0].style.color == style.color


def test_gutter_symbols_differ_and_fill_one_column() -> None:
    assert GUTTER_BAR != GUTTER_MARKER
    assert len(GUTTER_BAR) == len(GUTTER_MARKER) == 1


def test_gutter_is_a_bar_for_an_entry_the_cursor_isnt_on() -> None:
    assert gutter(marked=False) == f"{GUTTER_BAR} "


def test_gutter_is_the_marker_for_the_cursors_entry() -> None:
    assert gutter(marked=True) == f"{GUTTER_MARKER} "


def test_gutter_is_two_columns_whether_marked_or_not() -> None:
    assert len(gutter(marked=False)) == len(gutter(marked=True)) == 2


def read(arguments: dict[str, str] | None = None) -> ToolCall:
    """A running call to Read with ``arguments``, by default a file's path."""
    arguments = {"file_path": "a.py"} if arguments is None else arguments
    return tool_call_started(
        ToolCallStarted(turn_id="t", call_id="c", name="Read", arguments=arguments)
    )


def test_a_running_tool_calls_entry_spins_beside_its_summary() -> None:
    assert tool_call_entry(read(), None, 0.0) == ("◜ Read a.py", "tool")
    assert tool_call_entry(read(), None, 1 / SPINNER_FPS) == ("◠ Read a.py", "tool")


def test_a_tool_calls_entry_is_labelled_with_its_agent() -> None:
    assert tool_call_entry(read(), "agent-3", 0.0) == ("agent-3: ◜ Read a.py", "tool")


def test_a_finished_tool_calls_entry_counts_its_outputs_lines() -> None:
    finished = ToolCallFinished(turn_id="t", call_id="c", output="a\nb\nc")
    call = tool_call_finished(read(), finished)
    assert tool_call_entry(call, None, 0.0) == ("→ Read a.py ✓ 3 lines", "tool")
    assert call.output == "a\nb\nc"


def test_a_failed_tool_calls_entry_is_an_error() -> None:
    failed = ToolCallFinished(turn_id="t", call_id="c", output="boom", is_error=True)
    call = tool_call_finished(read(), failed)
    assert tool_call_entry(call, None, 0.0) == ("→ Read a.py ✗ 1 line", "error")


def ended(reason: TurnEndReason, usage: Usage | None = None) -> TurnFinished:
    return TurnFinished(turn_id="t", agent_id="agent-3", reason=reason, usage=usage)


@pytest.mark.parametrize(
    ("reason", "label", "kind"),
    [
        ("completed", "done", "rule"),
        ("cancelled", "stopped", "rule"),
        ("max_steps", "out of steps", "rule"),
        ("error", "error", "error_rule"),
    ],
)
def test_a_turns_rule_says_how_it_ended_in_its_kind(
    reason: TurnEndReason, label: str, kind: EntryKind
) -> None:
    assert turn_rule_entry(ended(reason), None, None) == (label, kind)


def test_a_turns_rule_is_headed_by_its_speaker() -> None:
    assert turn_rule_entry(ended("completed"), 250.0, "agent-3") == ("agent-3 done · 4m10s", "rule")


def test_a_turns_rule_without_usage_shows_the_time_elapsed() -> None:
    assert turn_rule_entry(ended("cancelled"), 42.7, None) == ("stopped · 42s", "rule")


def test_a_turns_rule_with_usage_shows_its_time_and_tokens() -> None:
    usage = Usage(
        input_tokens=200_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=50_000,
        output_tokens=8_400,
        duration_ms=72_500,
    )
    assert turn_rule_entry(ended("completed", usage), 5.0, None) == (
        "done · 1m12s · ↑1.2M ↓8.4k",
        "rule",
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (59.9, "59s"),
        (60, "1m00s"),
        (72, "1m12s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3780, "1h03m"),
    ],
)
def test_durations_read_in_seconds_minutes_or_hours(seconds: float, text: str) -> None:
    assert duration_text(seconds) == text


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (0, "0"),
        (812, "812"),
        (999, "999"),
        (1000, "1k"),
        (1250, "1.2k"),
        (8000, "8k"),
        (9999, "9.9k"),
        (10000, "10k"),
        (14_200, "14k"),
        (999_999, "999k"),
        (1_000_000, "1M"),
        (1_250_000, "1.2M"),
        (12_000_000, "12M"),
    ],
)
def test_token_counts_are_compact_and_rounded_down(count: int, text: str) -> None:
    assert token_text(count) == text


def test_usage_reads_input_with_the_cache_up_and_output_down() -> None:
    usage = Usage(
        input_tokens=10, cache_creation_input_tokens=20, cache_read_input_tokens=30, output_tokens=5
    )
    assert usage_text(usage) == "↑60 ↓5"


def test_only_rule_kinds_are_ruled() -> None:
    assert ruled("rule")
    assert ruled("error_rule")
    assert not ruled("tool")


def test_an_error_is_labelled_with_its_agent() -> None:
    assert error_entry(ErrorOccurred(agent_id="agent-1", message="boom")) == (
        "agent-1: boom",
        "error",
    )
    assert error_entry(ErrorOccurred(message="boom")) == ("boom", "error")


def test_only_the_message_after_the_last_tool_call_is_the_final_answer() -> None:
    assert final_answer(["message", "tool", "message"]) == (1,)


def test_a_turn_of_a_message_alone_has_it_as_its_final_answer() -> None:
    assert final_answer(["message"]) == (0,)


def test_a_turn_ending_on_a_tool_call_has_no_final_answer() -> None:
    assert final_answer(["message", "tool"]) == ()


def test_every_message_after_the_last_tool_call_is_the_final_answer() -> None:
    assert final_answer(["tool", "message", "tool", "message", "message"]) == (1, 2)


def test_a_turn_with_no_output_has_no_final_answer() -> None:
    assert final_answer([]) == ()


def test_the_conversation_title_names_the_tab_shown_after_all() -> None:
    assert conversation_title(None, False) == "Conversation"
    assert conversation_title("coder", False) == "Conversation · coder"


def test_the_conversation_title_notes_hidden_thinking() -> None:
    assert conversation_title(None, True) == "Conversation · thinking hidden"
    assert conversation_title("coder", True) == "Conversation · coder · thinking hidden"


def exited(reason: AgentExitReason) -> AgentExited:
    return AgentExited(agent_id="agent-3", reason=reason)


@pytest.mark.parametrize(
    ("reason", "text", "kind"),
    [
        ("finished", "agent-3 done · 42s", "rule"),
        ("killed", "agent-3 stopped · 42s", "rule"),
        ("crashed", "agent-3 crashed · 42s", "error_rule"),
    ],
)
def test_an_agent_exiting_without_a_turn_rule_adds_its_own(
    reason: AgentExitReason, text: str, kind: EntryKind
) -> None:
    assert exit_rule(exited(reason), None, 42.5, "agent-3") == ExitRule(text, kind, False)


def test_an_agents_own_exit_rule_shows_no_time_when_unknown() -> None:
    assert exit_rule(exited("crashed"), None, None, None) == ExitRule(
        "crashed", "error_rule", False
    )


def test_an_agent_finishing_after_its_turn_rule_adds_nothing() -> None:
    assert exit_rule(exited("finished"), DrawnRule(ended("completed"), 5.0), 9.0, "agent-3") is None


@pytest.mark.parametrize("reason", ["cancelled", "max_steps", "error"])
def test_an_agent_killed_after_a_rule_not_done_leaves_it(reason: TurnEndReason) -> None:
    assert exit_rule(exited("killed"), DrawnRule(ended(reason), 5.0), 9.0, "agent-3") is None


def test_an_agent_killed_after_a_done_rule_turns_it_stopped_keeping_its_cost() -> None:
    usage = Usage(input_tokens=210_000, output_tokens=12_000, duration_ms=250_000)
    drawn = DrawnRule(ended("completed", usage), 5.0)
    assert exit_rule(exited("killed"), drawn, 9.0, "agent-3") == ExitRule(
        "agent-3 stopped · 4m10s · ↑210k ↓12k", "rule", True
    )


def test_an_agent_crashing_after_a_done_rule_turns_it_a_crashed_error_rule() -> None:
    drawn = DrawnRule(ended("completed"), 42.0)
    assert exit_rule(exited("crashed"), drawn, 9.0, None) == ExitRule(
        "crashed · 42s", "error_rule", True
    )
