"""A tab's log of entries, without the list showing it: which entries it shows, how each is
drawn, its cursor's marker and what it inspects."""

from orbit.protocol import AgentReply, AgentRequest
from orbit.tui.view.log import Entry, EntryRow, entry_row
from orbit.tui.view.tool_calls import tool_call_started
from orbit.tui.view.transcript import GUTTER_BAR, GUTTER_MARKER, PROMPT_LINES_SHOWN
from tests.transcript_fakes import EntryList
from tests.tui_fakes import call, result


def prompt_of(lines: int) -> str:
    return "\n".join(f"line {i}" for i in range(lines))


def test_an_entry_is_drawn_as_its_label_beside_a_bar() -> None:
    assert entry_row(Entry("Task 3: **do** it", "user"), False, False, None) == EntryRow(
        text="Task 3: **do** it",
        kind="user",
        label="Task 3: **do** it",
        component="transcript--user",
        gutter=f"{GUTTER_BAR} ",
        gutter_component="transcript--gutter",
    )


def test_the_cursors_entry_is_drawn_with_the_marker() -> None:
    row = entry_row(Entry("hi", "agent"), False, True, None)
    assert (row.gutter, row.gutter_component) == (f"{GUTTER_MARKER} ", "transcript--marker")


def test_an_agents_message_is_markdown_dim_while_thinking_under_its_speakers_label() -> None:
    thinking = Entry("hi", "assistant", "agent-1", "t", speaker="agent-1")
    row = entry_row(thinking, False, False, None)
    assert (row.body, row.component, row.heading) == ("markdown", "transcript--thinking", "agent-1")
    assert entry_row(thinking, False, False, "agent-1").heading is None
    bright = Entry("hi", "assistant", "agent-1", speaker="agent-1")
    assert entry_row(bright, True, False, None).component == "transcript--assistant"
    assert entry_row(bright, True, False, None).body is None  # Collapsed, it's its first line.


def test_a_prompt_is_banded_markdown_and_a_rule_is_a_rule() -> None:
    prompt = entry_row(Entry("hi", "prompt"), False, False, None)
    assert (prompt.body, prompt.banded, prompt.label) == ("markdown", True, "❯ hi")
    rule = entry_row(Entry("agent-1 done", "error_rule"), False, False, None)
    assert (rule.body, rule.banded, rule.component) == ("rule", False, "transcript--error_rule")


def test_entries_are_shown_as_they_come_pinned_to_the_latest() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("Task 1: do it", "user"))
    log.append(Entry("plain", "assistant"))
    assert view.labels == ["Task 1: do it", "plain"]
    assert [row.component for row in view.rows] == ["transcript--user", "transcript--assistant"]
    assert view.ends == 2 and view.gutters == [GUTTER_BAR, GUTTER_BAR]


def test_an_active_log_or_one_holding_a_cursor_stays_where_it_is() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("a", "agent"))
    view.has_focus = True
    log.append(Entry("b", "agent"))
    view.has_focus = False
    view.move(0)
    log.append(Entry("c", "agent"))
    assert view.ends == 1 and view.labels == ["a", "b", "c"]


def test_a_prompt_longer_than_its_lines_shown_starts_collapsed() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry(prompt_of(PROMPT_LINES_SHOWN + 1), "prompt"))
    log.append(Entry(prompt_of(PROMPT_LINES_SHOWN), "prompt"))
    assert log.collapsed(0) and not log.collapsed(1)
    assert view.rows[0].body is None
    assert view.rows[0].label == f"❯ ▸ line 0 (+{PROMPT_LINES_SHOWN} lines)"
    assert view.rows[1].body == "markdown"


def test_a_prompt_toggles_between_collapsed_and_expanded() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("one\n\ntwo", "prompt"))
    log.toggle(0)
    assert log.collapsed(0) and view.rows[0].label == "❯ ▸ one (+2 lines)"
    assert view.rows[0].body is None
    log.toggle(0)
    assert not log.collapsed(0) and view.rows[0].body == "markdown"


def test_an_entry_of_one_line_stays_as_it_is_when_toggled() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("one", "tool"))
    before = view.rows[0]
    log.toggle(0)
    assert not log.collapsed(0) and view.rows[0] is before


def test_the_marker_follows_the_cursor() -> None:
    view = EntryList()
    log = view.entry_log
    for text in ("a", "b"):
        log.append(Entry(text, "agent"))
    view.move(1)
    assert view.gutters == [GUTTER_BAR, GUTTER_MARKER]
    view.move(0)
    assert view.gutters == [GUTTER_MARKER, GUTTER_BAR]
    view.move(None)
    assert view.gutters == [GUTTER_BAR, GUTTER_BAR]


def test_activating_starts_at_the_latest_entry_unless_a_cursor_is_held() -> None:
    view = EntryList()
    log = view.entry_log
    log.start_at_latest()
    assert view.highlighted is None
    for text in ("a", "b", "c"):
        log.append(Entry(text, "agent"))
    log.start_at_latest()
    assert view.highlighted == 2 and view.gutters == [GUTTER_BAR, GUTTER_BAR, GUTTER_MARKER]
    view.move(0)
    log.start_at_latest()
    assert view.highlighted == 0


def test_only_the_first_entry_is_at_the_top() -> None:
    view = EntryList()
    log = view.entry_log
    assert not log.at_top()
    for text in ("a", "b"):
        log.append(Entry(text, "agent"))
    view.move(1)
    assert not log.at_top()
    view.move(0)
    assert log.at_top()


def test_letting_go_of_the_cursor_follows_the_latest_entry_again() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("a", "agent"))
    view.move(0)
    log.release_cursor()
    assert view.highlighted is None and view.gutters == [GUTTER_BAR] and view.ends == 2


def test_replacing_a_rule_in_a_log_without_one_leaves_it() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("hi", "agent", "agent-3"))
    log.replace_rule("agent-3", "agent-3 stopped", "rule")
    assert log.entries == (Entry("hi", "agent", "agent-3"),)


def test_replacing_a_rule_redraws_it_where_it_is() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("agent-3 done", "rule", "agent-3"))
    log.append(Entry("hi", "agent", "agent-1"))
    log.replace_rule("agent-3", "agent-3 crashed", "error_rule")
    assert log.entries[0] == Entry("agent-3 crashed", "error_rule", "agent-3")
    assert view.labels == ["agent-3 crashed", "hi"] and view.rebuilds == 0


def test_dropping_an_entry_keeps_the_cursor_on_its_own_entry() -> None:
    view = EntryList()
    log = view.entry_log
    first, second, third = (Entry(text, "agent") for text in ("a", "b", "c"))
    for added in (first, second, third):
        log.append(added)
    view.move(2)
    log.drop(first)
    assert log.entries == (second, third)
    assert len(view.rows) == 2
    assert view.highlighted == 1 and view.gutters == [GUTTER_BAR, GUTTER_MARKER]


def test_dropping_the_held_entry_moves_the_cursor_to_the_one_before() -> None:
    view = EntryList()
    log = view.entry_log
    first, second = Entry("a", "agent"), Entry("b", "agent")
    log.append(first)
    log.append(second)
    view.move(1)
    log.drop(second)
    assert log.entries == (first,)
    assert view.highlighted == 0


def test_dropping_an_entry_without_a_cursor_leaves_none() -> None:
    view = EntryList()
    log = view.entry_log
    first = Entry("a", "agent")
    log.append(first)
    log.append(Entry("b", "agent"))
    log.drop(first)
    assert view.highlighted is None and view.labels == ["b"]


def test_a_call_after_a_dropped_entry_still_finishes() -> None:
    view = EntryList()
    log = view.entry_log
    removed = Entry("hi", "prompt")
    log.append(removed)
    log.append_call(tool_call_started(call("agent-1", "c1")), "agent-1", None)
    log.drop(removed)
    assert log.finish_call(result("agent-1", "c1", "done"))
    [finished] = log.entries
    assert finished.call is not None and finished.call.status != "running"
    assert not log.running


def test_dropping_an_entry_the_log_lacks_leaves_it() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("hi", "prompt"))
    log.drop(Entry("hi", "prompt"))  # Equal, but not the one added.
    assert log.entries == (Entry("hi", "prompt"),) and view.rebuilds == 0


def test_a_log_without_a_cursor_has_no_details() -> None:
    view = EntryList()
    log = view.entry_log
    log.append_call(tool_call_started(call("agent-1", "c1")), "agent-1", None)
    assert log.inspected is None and log.detail is None


def test_choosing_a_call_inspects_it_and_its_details_follow_the_cursor() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("hi", "agent"))
    log.append_call(tool_call_started(call("agent-1", "c1")), "agent-1", None)
    view.move(1)
    assert view.inspects == 0
    log.choose(1)
    assert log.inspecting and view.inspects == 1
    assert log.detail is not None and log.detail.call_id == "c1"
    view.move(0)
    assert view.inspects == 2 and log.inspected == 0 and log.detail is None


def test_choosing_any_other_entry_collapses_or_expands_it() -> None:
    view = EntryList()
    log = view.entry_log
    log.append(Entry("a\nb", "agent"))
    log.choose(0)
    assert log.collapsed(0) and not log.inspecting and view.inspects == 0


def test_a_call_finishing_while_inspected_says_so_once() -> None:
    view = EntryList()
    log = view.entry_log
    log.append_call(tool_call_started(call("agent-1", "c1")), "agent-1", None)
    log.append_call(tool_call_started(call("agent-1", "c2")), "agent-1", None)
    view.move(1)
    log.inspect(True)
    log.finish_call(result("agent-1", "c1", "x"))
    assert view.inspects == 1  # Another call than the one inspected.
    log.finish_call(result("agent-1", "c2", "y"))
    assert view.inspects == 2
    assert log.detail is not None and log.detail.output == "y"


def test_a_tick_while_inspecting_a_call_says_nothing() -> None:
    view = EntryList()
    log = view.entry_log
    log.append_call(tool_call_started(call("agent-1", "c1")), "agent-1", None)
    view.move(0)
    log.inspect(True)
    log.tick()
    assert view.inspects == 1


def test_stopping_inspecting_says_whether_the_log_was() -> None:
    view = EntryList()
    log = view.entry_log
    assert not log.stop_inspecting() and view.inspects == 0
    log.inspect(True)
    assert log.stop_inspecting() and not log.inspecting and view.inspects == 2


def test_leaving_the_log_stops_inspecting_unless_for_the_detail_pane() -> None:
    view = EntryList()
    log = view.entry_log
    log.blurred(False)
    assert view.inspects == 0
    log.inspect(True)
    log.blurred(True)
    assert log.inspecting and view.inspects == 1
    log.blurred(False)
    assert not log.inspecting and view.inspects == 2


def test_a_request_and_its_reply_are_one_entry() -> None:
    view = EntryList()
    log = view.entry_log
    ask = AgentRequest(agent_id="agent-1", request_id="r", tool="complete_task", arguments={})
    log.append_call(tool_call_started(ask), "agent-1", None)
    assert log.running
    assert not log.finish_call(AgentReply(agent_id="agent-2", request_id="r", text="ok"))
    assert log.finish_call(AgentReply(agent_id="agent-1", request_id="r", text="ok"))
    assert not log.running and view.labels == ["agent-1 → orbit complete_task ✓ ok"]
