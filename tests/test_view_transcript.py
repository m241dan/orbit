import json

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
    TurnFinished,
)
from orbit.tui.view import (
    GUTTER_BAR,
    GUTTER_MARKER,
    activity_entry,
    agent_entry,
    agent_labelled,
    collapsible,
    entry_label,
    entry_markdown,
    error_entry,
    exchange_entry,
    gutter,
    hanging_indent,
    note_entry,
    output_entry,
    shows_markdown,
    speaker_label,
    starts_collapsed,
    task_started_entry,
    turn_end_entry,
    turn_finished_entry,
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
    assert starts_collapsed(kind)


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


def test_agent_tool_calls_and_errors_are_labelled_with_their_agent() -> None:
    started = ToolCallStarted(
        turn_id="t", call_id="c", name="Read", arguments={}, agent_id="agent-2"
    )
    assert output_entry(started) == ("agent-2: → Read", "tool")
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
    end = turn_end_entry("cancelled")
    assert end is not None
    assert agent_labelled(end, "agent-2") == ("agent-2: [turn ended: cancelled]", "tool")
    assert agent_labelled(end, None) == end


def test_only_tool_and_request_entries_start_collapsed() -> None:
    assert starts_collapsed("tool") and starts_collapsed("request")
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


def test_only_expanded_assistant_entries_show_markdown() -> None:
    assert shows_markdown("assistant", collapsed=False)
    assert not shows_markdown("assistant", collapsed=True)
    assert not any(
        shows_markdown(kind, collapsed=False)
        for kind in ("user", "tool", "verify", "agent", "request", "error")
    )


def test_a_collapsible_entry_hangs_its_marker_beside_its_markdown() -> None:
    assert hanging_indent("one\ntwo") == ("▾ ", "  ")


def test_an_entry_of_one_line_has_no_hanging_indent() -> None:
    assert hanging_indent("one") == ("", "")


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


def test_an_unexpected_turn_ending_is_labelled_with_its_agent() -> None:
    cancelled = TurnFinished(turn_id="t", agent_id="agent-1", reason="cancelled")
    assert turn_finished_entry(cancelled) == ("agent-1: [turn ended: cancelled]", "tool")
    assert turn_finished_entry(TurnFinished(turn_id="t", reason="max_steps")) == (
        "[turn ended: max_steps]",
        "tool",
    )
    assert turn_finished_entry(TurnFinished(turn_id="t", agent_id="a", reason="completed")) is None


def test_an_error_is_labelled_with_its_agent() -> None:
    assert error_entry(ErrorOccurred(agent_id="agent-1", message="boom")) == (
        "agent-1: boom",
        "error",
    )
    assert error_entry(ErrorOccurred(message="boom")) == ("boom", "error")
