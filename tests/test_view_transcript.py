from orbit.protocol import (
    PLANNER,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
    Notice,
    ProgressNoted,
    TaskGaveUp,
    TaskMerged,
    ToolCallFinished,
    ToolCallStarted,
)
from orbit.tui.view import (
    GUTTER_BAR,
    GUTTER_MARKER,
    PROMPT_LINES_SHOWN,
    PROMPT_MARKER,
    REPLY_LIMIT,
    SPINNER_FPS,
    ToolCall,
    activity_entry,
    agent_entry,
    agent_labelled,
    banded,
    collapsible,
    conversation_title,
    entry_label,
    entry_prefix,
    error_entry,
    exchange_entry,
    gutter,
    hanging_indent,
    note_entry,
    output_entry,
    prompt_title,
    ruled,
    shows_markdown,
    speaker,
    speaker_label,
    starts_collapsed,
    tool_call_entry,
    tool_call_finished,
    tool_call_started,
    tool_call_unfinished,
)


def test_turn_output_becomes_transcript_entries() -> None:
    assert output_entry(AssistantMessage(turn_id="t", text="hi")) == ("hi", "assistant")
    assert output_entry(ToolCallFinished(turn_id="t", call_id="c", output="ok")) == ("ok", "tool")
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True)
    assert output_entry(failed) == ("no", "error")


def test_agent_lifecycle_becomes_transcript_entries() -> None:
    started = AgentStarted(agent_id="agent-1", role="coder", task_id="1")
    assert agent_entry(started) == ("▶ agent-1 (coder) started", "agent")
    gave_up = TaskGaveUp(agent_id="agent-1", task_id="1", reason="unclear spec")
    assert agent_entry(gave_up) == ("🏳 agent-1 gave up: unclear spec", "error")


def asked(arguments: dict[str, str] | None = None, tool: str = "complete_task") -> ToolCall:
    """agent-1's request to orbit's ``tool`` with ``arguments``, by default a summary."""
    arguments = {"summary": "Add a flag"} if arguments is None else arguments
    request = AgentRequest(agent_id="agent-1", request_id="r", tool=tool, arguments=arguments)
    return tool_call_started(request)


def replied(text: str, is_error: bool = False) -> ToolCall:
    reply = AgentReply(agent_id="agent-1", request_id="r", text=text, is_error=is_error)
    return tool_call_finished(asked(), reply)


def test_a_waiting_orbit_requests_entry_spins_beside_its_agent_and_summary() -> None:
    entry = tool_call_entry(asked(), "agent-1", 0.0)
    assert entry == ("◜ agent-1 → orbit complete_task Add a flag", "request")


def test_an_answered_orbit_requests_entry_shows_the_replys_first_line() -> None:
    entry = tool_call_entry(replied("Checks passed.\nMerged."), "agent-1", 0.0)
    assert entry == ("agent-1 → orbit complete_task Add a flag ✓ Checks passed.", "request")


def test_a_refused_orbit_requests_entry_is_an_error() -> None:
    entry = tool_call_entry(replied("No task.", is_error=True), "agent-1", 0.0)
    assert entry == ("agent-1 → orbit complete_task Add a flag ✗ No task.", "error")


def test_an_orbit_requests_reply_is_clipped_to_fit() -> None:
    text, _ = tool_call_entry(replied("a" * 100), "agent-1", 0.0)
    assert text.endswith(f" ✓ {'a' * (REPLY_LIMIT - 1)}…")


def test_an_empty_reply_leaves_only_the_tick() -> None:
    text, _ = tool_call_entry(replied(""), "agent-1", 0.0)
    assert text == "agent-1 → orbit complete_task Add a flag ✓"


def test_an_unanswered_orbit_request_has_no_reply() -> None:
    entry = tool_call_entry(tool_call_unfinished(asked()), "agent-1", 0.0)
    assert entry == ("agent-1 → orbit complete_task Add a flag ⊘ no reply", "tool")


def test_an_orbit_request_keeps_its_full_arguments_and_reply() -> None:
    call = replied("Checks passed.\nMerged.")
    assert call.arguments == {"summary": "Add a flag"}
    assert call.output == "Checks passed.\nMerged."


def test_an_orbit_request_summary_is_clipped_to_the_tool_call_limit() -> None:
    title = "a" * 200
    call = asked({"title": title}, tool="suggest_task")
    assert call.summary == f"suggest_task {title[:106]}…"


def test_an_orbit_request_without_arguments_shows_its_tool() -> None:
    assert asked({}, tool="get_task").summary == "get_task"


def test_a_reply_to_no_request_shown_is_an_entry_of_its_own() -> None:
    reply = AgentReply(agent_id="agent-1", request_id="r", text="Checks passed.")
    assert exchange_entry(reply) == ("orbit → agent-1: Checks passed.", "agent")
    refused = AgentReply(agent_id="agent-1", request_id="r", text="No task.", is_error=True)
    assert exchange_entry(refused) == ("orbit → agent-1: No task.", "error")


def test_a_progress_note_is_an_entry() -> None:
    note = ProgressNoted(agent_id="agent-1", task_id="1", note="tests written")
    assert exchange_entry(note) == ("agent-1: tests written", "agent")


def test_agent_tool_output_and_errors_are_labelled_with_their_agent() -> None:
    failed = ToolCallFinished(turn_id="t", call_id="c", output="no", is_error=True, agent_id="a")
    assert output_entry(failed) == ("a: no", "error")


def test_an_agents_message_leaves_its_agent_out_of_its_text() -> None:
    said = AssistantMessage(turn_id="t", text="on it", agent_id="agent-2")
    assert output_entry(said) == ("on it", "assistant")


def test_an_agents_message_is_labelled_when_the_speaker_changes() -> None:
    assert speaker_label("assistant", "agent-2", None) == "agent-2"
    assert speaker_label("assistant", "agent-2", "agent-1") == "agent-2"


def test_an_agents_message_after_its_own_entry_is_not_labelled() -> None:
    assert speaker_label("assistant", "agent-2", "agent-2") is None


ROLES = {"agent-1": PLANNER, "agent-2": "coder"}


def test_the_planners_messages_are_never_labelled() -> None:
    assert speaker_label("assistant", speaker("agent-1", ROLES), "agent-2") is None


def test_an_agent_speaks_for_itself() -> None:
    assert speaker("agent-2", ROLES) == "agent-2"
    assert speaker("agent-9", ROLES) == "agent-9"


def test_the_planner_its_prompts_and_entries_with_no_agent_are_one_speaker() -> None:
    assert speaker("agent-1", ROLES) is None
    assert speaker(PLANNER, ROLES) is None
    assert speaker(None, ROLES) is None


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


def test_only_tool_entries_start_collapsed() -> None:
    assert starts_collapsed("tool", "a\nb")
    assert not any(
        starts_collapsed(kind, "a\nb")
        for kind in ("user", "assistant", "verify", "agent", "request", "error")
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


def test_the_prompt_title_names_the_planner_and_notes_when_it_is_busy() -> None:
    assert prompt_title(False) == "Prompt · planner"
    assert prompt_title(True) == "Prompt · planner · busy"


def test_the_conversation_title_names_the_tab_shown_after_all() -> None:
    assert conversation_title(None, False) == "Conversation"
    assert conversation_title("coder", False) == "Conversation · coder"


def test_the_conversation_title_notes_hidden_thinking() -> None:
    assert conversation_title(None, True) == "Conversation · thinking hidden"
    assert conversation_title("coder", True) == "Conversation · coder · thinking hidden"
