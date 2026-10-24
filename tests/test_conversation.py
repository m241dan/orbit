"""The conversation, without the window showing it: the entries it keeps under each tab from
the events it's fed, which log is shown, the cursor and its marker, collapsing, hidden
thinking, and what runs."""

import pytest

from orbit.protocol import (
    AGENT_FAILED,
    PLAN_TASK,
    PLANNER,
    PLANNER_LANE,
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
    Event,
    Notice,
    RunningAgent,
    Snapshot,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
)
from orbit.tui.view.common import SPINNER_FPS, EntryKind
from orbit.tui.view.log import Entry
from orbit.tui.view.tool_calls import REPLY_LIMIT
from orbit.tui.view.transcript import GUTTER_BAR, GUTTER_MARKER
from tests.session_apps import OLD
from tests.transcript_fakes import Session
from tests.tui_fakes import call, result, said, started
from tests.tui_fakes import opened as snapshot_of


def tool_output(agent_id: str, output: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", call_id="c", output=output, agent_id=agent_id)


def thought(agent_id: str, text: str, turn: str = "t") -> AssistantMessage:
    return AssistantMessage(turn_id=turn, text=text, agent_id=agent_id)


def tool_call(agent_id: str, turn: str = "t") -> ToolCallStarted:
    return ToolCallStarted(turn_id=turn, call_id="c", name="Read", arguments={}, agent_id=agent_id)


def turn_began(agent_id: str, turn: str = "t") -> TurnStarted:
    return TurnStarted(turn_id=turn, agent_id=agent_id)


def turn_ended(agent_id: str, turn: str = "t", reason: TurnEndReason = "completed") -> TurnFinished:
    return TurnFinished(turn_id=turn, agent_id=agent_id, reason=reason)


def texts(session: Session, key: str | None) -> list[tuple[str, EntryKind]]:
    return [(entry.text, entry.kind) for entry in session.conversation.entries(key)]


def test_entries_are_kept_under_all_and_under_their_tab() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "hi"), Notice(message="n"))
    conversation = session.conversation
    assert conversation.entries(None) == (
        Entry("▶ agent-1 (coder) started", "agent", "agent-1", speaker="agent-1"),
        Entry("hi", "assistant", "agent-1", "t", speaker="agent-1"),
        Entry("📌 n", "verify"),
    )
    # agent-1 runs in a pinned role, so its entries belong to the role's tab.
    assert conversation.entries("coder") == conversation.entries(None)[:2]
    assert conversation.entries("agent-1") == ()


def test_entries_are_labelled_beside_a_dim_bar_without_a_cursor() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "hi\n\nthere"))
    log = session.shown
    assert log.labels == ["▶ agent-1 (coder) started", "▾ hi\n\nthere"]
    assert log.highlighted is None and log.gutters == [GUTTER_BAR, GUTTER_BAR]
    assert {row.gutter_component for row in log.rows} == {"transcript--gutter"}


def test_the_cursors_entry_is_marked_and_the_marker_moves_with_it() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "hi\nthere"))
    session.activate()
    log = session.shown
    assert log.gutters == [GUTTER_BAR, GUTTER_MARKER]
    assert log.rows[1].gutter_component == "transcript--marker"
    session.move(0)
    assert log.gutters == [GUTTER_MARKER, GUTTER_BAR]
    session.move(1)
    assert log.gutters == [GUTTER_BAR, GUTTER_MARKER]


def test_a_toggled_entry_keeps_its_marker() -> None:
    session = Session()
    session.events(started("agent-1"), tool_output("agent-1", "a\nb"))
    session.activate()
    session.choose()
    assert session.shown.labels[1] == "▾ agent-1: a\nb"
    assert session.shown.gutters == [GUTTER_BAR, GUTTER_MARKER]


def test_the_marker_stays_after_leaving_and_goes_with_the_cursor() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "hi"))
    session.activate()
    session.escape()
    log = session.shown
    assert not log.has_focus and log.highlighted == 1
    assert log.gutters == [GUTTER_BAR, GUTTER_MARKER]
    session.move(None)
    assert log.gutters == [GUTTER_BAR, GUTTER_BAR]


def test_tool_output_of_several_lines_starts_collapsed() -> None:
    session = Session()
    session.events(
        started("agent-1"),
        tool_output("agent-1", "one line"),
        tool_output("agent-1", "first\nsecond\nthird"),
        tool_output("agent-1", "first\nsecond"),
    )
    log = session.shown
    assert log.labels[1:] == [
        "agent-1: one line",
        "▸ agent-1: first (+2 lines)",
        "▸ agent-1: first (+1 line)",
    ]
    assert [log.entry_log.collapsed(i) for i in range(4)] == [False, False, True, True]


def test_the_conversation_can_be_activated_to_move_between_entries_and_toggle_them() -> None:
    session = Session()
    session.events(started("agent-1"), tool_output("agent-1", "first\nsecond"))
    session.activate()
    log = session.shown
    assert log.highlighted == 1  # The latest entry.
    session.choose()
    assert log.labels[1] == "▾ agent-1: first\nsecond"
    session.choose()
    assert log.labels[1] == "▸ agent-1: first (+1 line)"
    session.move(0)
    session.choose()  # A one-line entry has nothing to collapse.
    assert log.highlighted == 0 and log.labels[0] == "▶ agent-1 (coder) started"
    session.escape()
    assert not log.has_focus
    session.activate()  # Activated again, it picks up where it was.
    assert log.highlighted == 0


def test_the_shown_log_stays_pinned_to_the_latest_entry_unless_it_has_a_cursor() -> None:
    session = Session()
    session.events(*(said("agent-1", f"line {i}") for i in range(40)))
    log = session.shown
    assert log.ends == 40
    session.activate()
    assert log.highlighted == 39
    session.move(0)
    session.events(said("agent-1", "later"))
    assert log.ends == 40 and log.highlighted == 0
    session.escape()
    session.release()
    assert log.ends == 41
    session.events(said("agent-1", "latest"))
    assert log.ends == 42


def test_switching_tabs_scrolls_the_tab_shown_to_its_place() -> None:
    session = Session()
    session.events(said("agent-1", "a"))
    everything = session.shown
    session.show_only(PLANNER, PLANNER)
    session.show_only(None)
    assert session.shown is everything and everything.places == 1


def test_a_tab_shows_only_its_entries_and_all_shows_every_one() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "coding"))
    session.events(started("agent-2", "planner"), said("agent-2", "planning"))
    window = session.window
    assert len(window.logs) == 3  # All, and a log per tab with entries.
    session.show_only("planner", "planner")
    assert session.conversation.only == "planner"
    assert session.shown.labels == ["▶ agent-2 (planner) started", "planning"]
    session.show_only("coder", "coder")
    assert session.shown.labels == ["▶ agent-1 (coder) started", "coding"]
    session.show_only("reviewer", "reviewer")  # No entries, so its log is built now.
    assert session.shown.labels == []
    assert len(window.logs) == 4
    session.show_only(None)
    assert session.conversation.only is None
    assert len(session.shown.labels) == 4


def test_showing_the_shown_tab_again_keeps_its_log_shown_and_retitles_it() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "coding"))
    session.show_only("coder", "coder")
    log = session.shown
    session.show_only("coder", "● coder")
    assert session.window.displayed == [log]
    assert session.window.title == "Conversation · ● coder"


def test_output_for_a_tab_not_shown_is_there_when_it_is() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "first"))
    session.show_only("coder", "coder")
    assert session.shown.labels == ["▶ agent-1 (coder) started", "first"]
    session.show_only(None)
    session.events(said("agent-1", "second"))
    session.show_only("coder", "coder")
    assert session.shown.labels[-1] == "second"


def test_switching_tabs_displays_only_the_shown_log() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "first"))
    window = session.window
    everything = session.shown
    assert window.displayed == [everything]
    session.show_only("coder", "coder")
    coder = session.shown
    assert coder is not everything and window.displayed == [coder]
    session.show_only(None)
    assert session.shown is everything and window.displayed == [everything]
    session.show_only("coder", "coder")
    assert window.displayed == [coder]


def test_letting_go_with_no_entries_does_nothing_harmful() -> None:
    session = Session()
    session.activate()
    session.escape()
    session.release()
    log = session.shown
    assert log.highlighted is None and log.rows == []


def test_each_tab_holds_its_own_cursor() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "a"))
    session.activate()
    session.move(0)
    session.escape()  # All holds its first entry.
    session.show_only("coder", "coder")
    coder = session.shown
    assert coder.highlighted is None
    session.activate()
    session.escape()
    session.release()  # Held, then let go, in coder only.
    assert coder.highlighted is None
    session.activate()
    session.escape()  # Held again in coder.
    session.show_only(None)  # All, which kept its cursor.
    assert session.shown.highlighted == 0
    session.release()  # Lets go of All's only.
    assert session.shown.highlighted is None
    assert coder.highlighted == 1


def test_switching_tabs_away_from_an_active_log_leaves_it_with_its_cursor() -> None:
    session = Session()
    session.events(*(said("agent-1", f"line {i}") for i in range(3)))
    session.activate()
    session.move(0)
    session.show_only(PLANNER, PLANNER)
    assert session.window.unfocused == 1
    session.show_only(None)
    assert session.window.unfocused == 1  # Nothing was active.
    assert session.shown.highlighted == 0 and not session.shown.has_focus


def lines(session: Session) -> tuple[str, ...]:
    """The working row's lines, read afresh each time."""
    return session.window.working.lines


def headings(session: Session) -> list[str | None]:
    return session.shown.headings


def test_an_agents_first_message_is_headed_by_a_label_row() -> None:
    session = Session()
    session.events(said("agent-1", "hi"))
    assert headings(session) == ["agent-1"]


def test_an_agents_messages_in_a_row_are_labelled_once() -> None:
    session = Session()
    session.events(
        said("agent-1", "looking"), tool_output("agent-1", "found"), said("agent-1", "x")
    )
    assert headings(session) == ["agent-1", None, None]


def test_another_agent_in_between_brings_the_label_back() -> None:
    session = Session()
    session.events(said("agent-1", "first"), said("agent-2", "second"), said("agent-1", "third"))
    assert headings(session) == ["agent-1", "agent-2", "agent-1"]


def test_the_planners_messages_are_never_labelled() -> None:
    session = Session()
    session.prompt("hi")
    session.events(
        AssistantMessage(turn_id="t", text="planning"),
        said("agent-1", "coding"),
        AssistantMessage(turn_id="t", text="planned"),
    )
    assert headings(session) == [None, None, "agent-1", None]


def test_a_real_planners_messages_are_never_labelled() -> None:
    session = Session()
    session.events(started("agent-1", "planner"), started("agent-2"))
    session.prompt("hi")
    session.events(
        said("agent-1", "planning"), said("agent-2", "coding"), said("agent-1", "planned")
    )
    assert headings(session) == [None, None, None, None, "agent-2", None]
    session.show_only(PLANNER, PLANNER)
    assert session.shown.labels == ["▶ agent-1 (planner) started", "❯ hi", "planning", "planned"]
    assert headings(session) == [None, None, None, None]


def test_the_planners_message_after_a_prompt_has_no_heading() -> None:
    session = Session()
    session.events(AgentStarted(agent_id="agent-1", role=PLANNER, task_id=PLAN_TASK))
    session.prompt("hi")
    session.events(said("agent-1", "plan"), turn_ended("agent-1"))
    assert session.shown.labels == ["▶ agent-1 (planner) started", "❯ hi", "plan", "done"]
    assert headings(session) == [None, None, None, None]


def test_the_planners_message_has_no_heading_when_it_starts_after_the_prompt() -> None:
    session = Session()
    session.prompt("hi")
    session.events(
        AgentStarted(agent_id="agent-1", role=PLANNER, task_id=PLAN_TASK),
        said("agent-1", "plan"),
        said("agent-2", "code"),
        said("agent-1", "more"),
    )
    begun = "▶ agent-1 (planner) started"
    assert session.shown.labels == ["❯ hi", begun, "plan", "code", "more"]
    assert headings(session) == [None, None, None, "agent-2", None]
    session.show_only(PLANNER, PLANNER)
    assert session.shown.labels == ["❯ hi", begun, "plan", "more"]
    assert headings(session) == [None, None, None, None]


def test_the_speaker_is_judged_within_each_tab() -> None:
    session = Session()
    session.events(
        started("agent-1"),
        started("agent-2", "reviewer"),
        said("agent-1", "coding"),
        said("agent-2", "reviewing"),
        said("agent-1", "more"),
    )
    # Under All the reviewer speaks in between; under coder agent-1 speaks throughout.
    assert headings(session) == [None, None, "agent-1", "agent-2", "agent-1"]
    session.show_only("coder", "coder")
    assert session.shown.labels == ["▶ agent-1 (coder) started", "coding", "more"]
    assert headings(session) == [None, None, None]


def test_a_collapsed_agents_message_keeps_its_label_row() -> None:
    session = Session()
    session.events(said("agent-1", "one\ntwo"))
    log = session.shown
    log.entry_log.toggle(0)
    assert log.entry_log.collapsed(0) and log.rows[0].body is None
    assert headings(session) == ["agent-1"]


def test_an_agents_message_arrives_dim_under_a_label_row() -> None:
    session = Session()
    session.events(thought("agent-1", "looking"))
    assert session.shown.brightness == ["dim"] and headings(session) == ["agent-1"]


def test_only_the_message_after_the_last_tool_call_brightens_when_the_turn_ends() -> None:
    session = Session()
    session.events(
        thought("agent-1", "looking"),
        tool_call("agent-1"),
        tool_output("agent-1", "found"),
        thought("agent-1", "done"),
    )
    assert session.shown.brightness == ["dim", "dim"]
    session.events(turn_ended("agent-1"))
    assert session.shown.brightness == ["dim", "bright"]


@pytest.mark.parametrize("reason", ["completed", "cancelled", "error", "max_steps"])
def test_a_turn_of_a_message_alone_brightens_it_however_it_ends(reason: TurnEndReason) -> None:
    session = Session()
    session.events(thought("agent-1", "done"), turn_ended("agent-1", reason=reason))
    assert session.shown.brightness == ["bright"]


def test_a_turn_ending_on_a_tool_call_brightens_nothing() -> None:
    session = Session()
    session.events(thought("agent-1", "looking"), tool_call("agent-1"), turn_ended("agent-1"))
    assert session.shown.brightness == ["dim"]


def test_every_message_after_the_last_tool_call_brightens() -> None:
    session = Session()
    session.events(
        tool_call("agent-1"),
        thought("agent-1", "found it"),
        thought("agent-1", "done"),
        turn_ended("agent-1"),
    )
    assert session.shown.brightness == ["bright", "bright"]


@pytest.mark.parametrize("first", ["agent-1", "agent-2"])
def test_concurrent_turns_brighten_only_their_own_messages(first: str) -> None:
    session = Session()
    turns = {"agent-1": "t1", "agent-2": "t2"}
    session.events(
        thought("agent-1", "one", "t1"),
        thought("agent-2", "two", "t2"),
        turn_ended(first, turns[first]),
    )
    expected = {"agent-1": ["bright", "dim"], "agent-2": ["dim", "bright"]}[first]
    assert session.shown.brightness == expected
    last = "agent-2" if first == "agent-1" else "agent-1"
    session.events(turn_ended(last, turns[last]))
    assert session.shown.brightness == ["bright", "bright"]


@pytest.mark.parametrize("reason", ["killed", "crashed"])
def test_an_agent_exiting_mid_turn_brightens_its_final_answer(reason: AgentExitReason) -> None:
    session = Session()
    session.events(thought("agent-1", "done"), AgentExited(agent_id="agent-1", reason=reason))
    assert session.shown.brightness == ["bright"]


def test_an_agent_exiting_mid_turn_on_a_tool_call_brightens_nothing() -> None:
    session = Session()
    session.events(
        thought("agent-1", "looking"),
        tool_call("agent-1"),
        AgentExited(agent_id="agent-1", reason="crashed"),
    )
    assert session.shown.brightness == ["dim"]


def test_an_agent_exiting_after_its_turn_ended_keeps_its_final_answer_bright() -> None:
    session = Session()
    session.events(
        thought("agent-1", "done"),
        turn_ended("agent-1"),
        AgentExited(agent_id="agent-1", reason="finished"),
    )
    assert session.shown.brightness == ["bright"]


def test_an_agent_exiting_leaves_another_agents_open_turn_dim() -> None:
    session = Session()
    session.events(
        thought("agent-1", "one", "t1"),
        thought("agent-2", "two", "t2"),
        AgentExited(agent_id="agent-1", reason="killed"),
    )
    assert session.shown.brightness == ["bright", "dim"]


def test_a_final_answer_brightens_under_all_and_its_roles_tab() -> None:
    session = Session()
    session.events(
        started("agent-1"),
        started("agent-2", "reviewer"),
        thought("agent-1", "done", "t1"),
        thought("agent-2", "reviewing", "t2"),
        turn_ended("agent-1", "t1"),
    )
    assert session.shown.brightness == ["bright", "dim"]
    session.show_only("coder", "coder")
    assert session.shown.brightness == ["bright"]
    done = Entry("done", "assistant", "agent-1", speaker="agent-1")
    assert session.conversation.entries("coder")[1] == done


TOOL_ROWS = ["agent-1: → Read ✓ 1 line"]
"""How a thinking turn's tool call, finished, is labelled."""


def thinking_turn(agent_id: str = "agent-1", turn: str = "t") -> list[Event]:
    """A turn thinking out loud before a tool call and after its output, not yet ended."""
    return [
        thought(agent_id, "looking", turn),
        tool_call(agent_id, turn),
        tool_output(agent_id, "found"),
        thought(agent_id, "done", turn),
    ]


def test_hiding_thinking_leaves_out_dim_messages_and_showing_it_brings_them_back() -> None:
    session = Session()
    session.events(*thinking_turn())
    shown = session.shown.labels
    session.toggle_thinking()
    assert session.conversation.thinking_hidden and session.shown.labels == TOOL_ROWS
    session.toggle_thinking()
    assert not session.conversation.thinking_hidden and session.shown.labels == shown


def test_hidden_thinking_leaves_bright_messages_shown() -> None:
    session = Session()
    session.events(*thinking_turn(), turn_ended("agent-1"))
    session.toggle_thinking()
    assert session.shown.labels == [*TOOL_ROWS, "done", "agent-1 done"]
    assert session.shown.brightness == ["bright"]


def test_hiding_thinking_applies_to_a_roles_tab() -> None:
    session = Session()
    session.events(started("agent-1"), *thinking_turn())
    session.show_only("coder", "coder")
    session.toggle_thinking()
    assert session.shown.labels == ["▶ agent-1 (coder) started", *TOOL_ROWS]


def test_a_tab_opened_while_thinking_is_hidden_hides_it_too() -> None:
    session = Session()
    session.toggle_thinking()
    session.events(started("agent-1"), *thinking_turn())
    session.show_only("coder", "coder")  # Its log was made after thinking was hidden.
    assert session.shown.labels == ["▶ agent-1 (coder) started", *TOOL_ROWS]


def test_the_title_notes_hidden_thinking_on_all_and_on_a_tab() -> None:
    session = Session()
    assert session.window.title == "Conversation"
    session.toggle_thinking()
    assert session.window.title == "Conversation · thinking hidden"
    session.show_only("coder", "coder")
    assert session.window.title == "Conversation · coder · thinking hidden"
    session.toggle_thinking()
    assert session.window.title == "Conversation · coder"


def test_the_title_follows_the_tabs_label_and_ends_with_the_session() -> None:
    session = Session()
    session.show_only("coder", "coder")
    session.conversation.relabel("● coder")
    assert session.window.title == "Conversation · ● coder"
    session.conversation.show_session("auth rework")
    assert session.window.title == "Conversation · ● coder — auth rework"
    session.show_only(None)
    session.conversation.show_session(None)
    assert session.window.title == "Conversation"


def test_a_hidden_cursors_entry_moves_the_cursor_to_the_entry_before_it() -> None:
    session = Session()
    session.events(tool_output("agent-1", "found"), *thinking_turn())
    session.activate()  # At the latest entry, a dim message.
    session.toggle_thinking()
    log = session.shown
    assert log.highlighted == 1 and log.labels[1] == "agent-1: → Read ✓ 1 line"
    assert log.gutters == [GUTTER_BAR, GUTTER_MARKER]


def test_a_hidden_cursors_entry_with_none_before_it_moves_to_the_one_after() -> None:
    session = Session()
    session.events(*thinking_turn())
    session.activate()
    session.move(0)  # At the first entry, a dim message.
    session.toggle_thinking()
    assert session.shown.highlighted == 0 and session.shown.gutters == [GUTTER_MARKER]


def test_a_hidden_cursors_entry_with_no_entry_left_leaves_no_cursor() -> None:
    session = Session()
    session.events(thought("agent-1", "looking"))
    session.activate()
    session.toggle_thinking()
    assert session.shown.rows == [] and session.shown.highlighted is None


def test_the_cursor_stays_on_its_entry_when_thinking_is_shown_again() -> None:
    session = Session()
    session.events(*thinking_turn())
    session.toggle_thinking()
    session.activate()  # On the tool call, the latest entry shown.
    session.toggle_thinking()
    assert session.shown.highlighted == 1
    assert session.shown.gutters == [GUTTER_BAR, GUTTER_MARKER, GUTTER_BAR]


def test_collapsed_entries_stay_so_across_hiding_and_showing_thinking() -> None:
    session = Session()
    session.events(thought("agent-1", "one\ntwo"), turn_ended("agent-1"))
    session.shown.entry_log.toggle(0)
    session.toggle_thinking()
    session.toggle_thinking()
    assert session.shown.entry_log.collapsed(0)


def test_a_message_promoted_while_thinking_is_hidden_appears() -> None:
    session = Session()
    session.events(*thinking_turn())
    session.toggle_thinking()
    session.events(turn_ended("agent-1"))
    assert session.shown.labels == [*TOOL_ROWS, "done", "agent-1 done"]
    assert session.shown.brightness == ["bright"]


def test_a_latest_message_promoted_while_hidden_is_appended_without_a_rebuild() -> None:
    session = Session()
    session.events(*thinking_turn())
    session.toggle_thinking()
    session.activate()  # On the tool call, the latest entry shown.
    log = session.shown
    first, rebuilds = log.rows[0], log.rebuilds
    session.conversation.promote("agent-1", "t", [1])  # "done", its final answer.
    assert log.labels == [*TOOL_ROWS, "done"]
    assert log.rows[0] is first and log.rebuilds == rebuilds
    assert log.highlighted == 0 and log.gutters == [GUTTER_MARKER, GUTTER_BAR]


def test_a_message_promoted_before_an_entry_shown_while_hidden_takes_its_place() -> None:
    session = Session()
    session.events(*thinking_turn())
    session.toggle_thinking()
    session.events(
        thought("agent-2", "reviewing", "t2"), turn_ended("agent-2", "t2"), turn_ended("agent-1")
    )
    assert session.shown.labels == [
        *TOOL_ROWS,
        "done",
        "reviewing",
        "agent-2 done",
        "agent-1 done",
    ]
    assert session.shown.brightness == ["bright", "bright"]


def test_a_turn_ending_on_a_tool_call_while_hidden_shows_only_its_rule() -> None:
    session = Session()
    session.events(thought("agent-1", "looking"), tool_call("agent-1"))
    session.toggle_thinking()
    session.events(turn_ended("agent-1"))
    assert session.shown.labels == ["agent-1: → Read ⊘ no result", "agent-1 done"]


def test_speaker_labels_are_judged_on_the_entries_shown() -> None:
    session = Session()
    session.events(
        thought("agent-1", "first", "t1"),
        turn_ended("agent-1", "t1"),
        thought("agent-2", "thinking", "t2"),
        thought("agent-1", "second", "t3"),
        turn_ended("agent-1", "t3"),
    )
    assert headings(session) == ["agent-1", None, "agent-2", "agent-1", None]
    session.toggle_thinking()
    assert headings(session) == ["agent-1", None, None, None]
    session.toggle_thinking()
    assert headings(session) == ["agent-1", None, "agent-2", "agent-1", None]


def test_speaker_labels_are_judged_on_the_entries_appended_while_hidden() -> None:
    session = Session()
    session.toggle_thinking()
    session.events(thought("agent-1", "first", "t1"))
    session.conversation.promote("agent-1", "t1", [0])
    session.events(thought("agent-2", "thinking", "t2"), thought("agent-1", "second", "t3"))
    session.conversation.promote("agent-1", "t3", [0])
    assert session.shown.labels == ["first", "second"]
    assert headings(session) == ["agent-1", None]


def test_a_turns_rule_follows_its_last_entry_timed_from_its_start() -> None:
    session = Session()
    session.window.now = 100.0
    session.events(started("agent-3"), turn_began("agent-3"))
    session.window.now = 350.0
    session.events(thought("agent-3", "done"), turn_ended("agent-3"))
    rule = Entry("agent-3 done · 4m10s", "rule", "agent-3", speaker="agent-3")
    for key in (None, "coder"):
        assert session.conversation.entries(key)[-2:] == (
            Entry("done", "assistant", "agent-3", speaker="agent-3"),
            rule,
        )


def test_a_turns_rule_with_usage_shows_its_time_and_tokens_and_counts_them() -> None:
    session = Session()
    usage = Usage(input_tokens=210_000, output_tokens=12_000, duration_ms=72_000)
    ended = TurnFinished(turn_id="t", agent_id="agent-3", reason="completed", usage=usage)
    session.events(turn_began("agent-3"), ended)
    assert session.shown.labels == ["agent-3 done · 1m12s · ↑210k ↓12k"]
    assert session.tasks.counted == [usage]


def test_a_turn_whose_error_was_shown_still_ends_in_an_error_rule() -> None:
    session = Session()
    session.events(
        ErrorOccurred(agent_id="agent-3", code=AGENT_FAILED, message="boom"),
        turn_ended("agent-3", reason="error"),
    )
    assert texts(session, None) == [("agent-3: boom", "error"), ("agent-3 error", "error_rule")]


@pytest.mark.parametrize(
    ("reason", "component"),
    [("completed", "rule"), ("max_steps", "rule"), ("error", "error_rule")],
)
def test_a_turns_rule_is_drawn_as_a_rule_in_its_style(
    reason: TurnEndReason, component: str
) -> None:
    session = Session()
    session.events(turn_ended("agent-3", reason=reason))
    [row] = session.shown.rows
    assert (row.body, row.component) == ("rule", f"transcript--{component}")


def test_an_agent_finishing_after_its_turn_ends_in_that_one_rule() -> None:
    session = Session()
    exit_ = AgentExited(agent_id="agent-3", reason="finished")
    session.events(started("agent-3"), turn_began("agent-3"), turn_ended("agent-3"), exit_)
    assert texts(session, None) == [
        ("▶ agent-3 (coder) started", "agent"),
        ("agent-3 done · 0s", "rule"),
    ]


def test_an_agent_crashing_mid_turn_ends_in_a_crashed_rule_timed_from_the_turn() -> None:
    session = Session()
    session.window.now = 100.0
    session.events(started("agent-3"), turn_ended("agent-3"))
    session.window.now = 120.0
    session.events(turn_began("agent-3", "t2"))
    session.window.now = 162.0
    session.events(AgentExited(agent_id="agent-3", reason="crashed"))
    assert texts(session, "coder")[-2:] == [
        ("agent-3 done", "rule"),
        ("agent-3 crashed · 42s", "error_rule"),
    ]


def test_an_agent_exiting_before_any_turn_is_timed_from_its_start() -> None:
    session = Session()
    session.window.now = 10.0
    session.events(started("agent-3"))
    session.window.now = 82.0
    session.events(AgentExited(agent_id="agent-3", reason="killed"))
    assert texts(session, None)[-1] == ("agent-3 stopped · 1m12s", "rule")


def test_an_agent_exiting_unseen_ends_in_an_untimed_rule() -> None:
    session = Session()
    session.events(AgentExited(agent_id="agent-3", reason="finished"))
    assert texts(session, None) == [("agent-3 done", "rule")]


def test_an_agent_killed_after_a_done_rule_turns_that_rule_stopped() -> None:
    session = Session()
    session.events(started("agent-3"), turn_began("agent-3"))
    session.events(turn_ended("agent-3"), said("agent-1", "elsewhere"))
    session.events(AgentExited(agent_id="agent-3", reason="killed"))
    for key in (None, "coder"):
        assert ("agent-3 stopped · 0s", "rule") in texts(session, key)
        assert ("agent-3 done · 0s", "rule") not in texts(session, key)
    assert session.shown.labels[-2:] == ["agent-3 stopped · 0s", "elsewhere"]


def test_a_temporary_tab_agents_exit_rule_lands_in_its_tab() -> None:
    session = Session()
    session.window.now = 10.0
    session.events(started("agent-3", "checker"))
    session.window.now = 52.0
    session.events(AgentExited(agent_id="agent-3", reason="crashed"))
    rule = ("agent-3 crashed · 42s", "error_rule")
    assert texts(session, "agent-3")[-1] == rule
    assert texts(session, None)[-1] == rule


def test_a_tool_call_and_its_result_are_one_entry_updated_in_place() -> None:
    session = Session()
    session.events(call("agent-1", "c1"))
    assert session.shown.labels == ["agent-1: ◜ Read a.py"]
    session.events(result("agent-1", "c1", "one\ntwo"))
    assert session.shown.labels == ["agent-1: → Read a.py ✓ 2 lines"]
    assert session.shown.rows[0].component == "transcript--tool"


def test_a_tool_calls_entry_keeps_its_full_arguments_and_output() -> None:
    session = Session()
    session.events(call("agent-1", "c1"), result("agent-1", "c1", "x\ny"))
    kept = session.conversation.entries(None)[0].call
    assert kept is not None
    assert (kept.arguments, kept.output) == ({"file_path": "a.py"}, "x\ny")


def test_a_failed_tool_call_is_crossed_in_the_error_style() -> None:
    session = Session()
    session.events(call("agent-1", "c1"), result("agent-1", "c1", "boom", is_error=True))
    assert session.shown.labels == ["agent-1: → Read a.py ✗ 1 line"]
    assert session.shown.rows[0].component == "transcript--error"


def test_concurrent_tool_calls_finishing_out_of_order_update_their_own_entries() -> None:
    session = Session()
    session.events(
        call("agent-1", "c1", "a.py"), call("agent-1", "c2", "b.py"), result("agent-1", "c2", "")
    )
    assert session.shown.labels == ["agent-1: ◜ Read a.py", "agent-1: → Read b.py ✓ 0 lines"]
    session.events(result("agent-1", "c1", "x"))
    assert session.shown.labels == [
        "agent-1: → Read a.py ✓ 1 line",
        "agent-1: → Read b.py ✓ 0 lines",
    ]


def test_a_result_updates_only_the_call_of_its_own_agent() -> None:
    session = Session()
    session.events(call("agent-1", "c"), call("agent-2", "c"), result("agent-2", "c", ""))
    assert session.shown.labels == ["agent-1: ◜ Read a.py", "agent-2: → Read a.py ✓ 0 lines"]


def test_a_tool_call_unfinished_when_its_turn_ends_has_no_result() -> None:
    session = Session()
    session.events(call("agent-1", "c1"), call("agent-2", "c2"), turn_ended("agent-1"))
    assert session.shown.labels[0] == "agent-1: → Read a.py ⊘ no result"
    assert session.shown.labels[1] == "agent-2: ◜ Read a.py"
    assert session.window.ticking


def test_a_tool_call_unfinished_when_its_agent_exits_has_no_result() -> None:
    session = Session()
    session.events(call("agent-1", "c1"), AgentExited(agent_id="agent-1", reason="crashed"))
    assert session.shown.labels[0] == "agent-1: → Read a.py ⊘ no result"
    assert not session.window.ticking


def test_a_result_after_its_turn_ended_is_an_entry_of_its_own() -> None:
    session = Session()
    session.events(call("agent-1", "c1"), turn_ended("agent-1"), result("agent-1", "c1", "x"))
    assert session.shown.labels == [
        "agent-1: → Read a.py ⊘ no result",
        "agent-1 done",
        "agent-1: x",
    ]


def test_a_tool_call_updates_under_all_and_its_roles_tab() -> None:
    session = Session()
    session.events(started("agent-1"), call("agent-1", "c1"), result("agent-1", "c1", "x"))
    assert session.shown.labels[1] == "agent-1: → Read a.py ✓ 1 line"
    session.show_only("coder", "coder")
    assert session.shown.labels == ["▶ agent-1 (coder) started", "agent-1: → Read a.py ✓ 1 line"]


def test_a_tool_call_finishing_after_hidden_thinking_updates_its_own_row() -> None:
    session = Session()
    session.toggle_thinking()
    session.events(
        thought("agent-1", "looking"),
        tool_output("agent-1", "earlier"),
        call("agent-1", "c1"),
        result("agent-1", "c1", "x"),
    )
    assert session.shown.labels == ["agent-1: earlier", "agent-1: → Read a.py ✓ 1 line"]


def test_a_running_tool_calls_spinner_turns_on_each_tick() -> None:
    session = Session()
    session.events(Notice(message="n"), call("agent-1", "c1"))
    assert session.window.ticking
    session.window.now = 1 / SPINNER_FPS
    session.conversation.tick()
    assert session.shown.labels == ["📌 n", "agent-1: ◠ Read a.py"]


def test_spinners_stop_once_no_tool_call_runs() -> None:
    session = Session()
    session.events(call("agent-1", "c1"), result("agent-1", "c1", "x"))
    assert not session.window.ticking
    session.window.now = 1 / SPINNER_FPS
    session.conversation.tick()
    assert session.shown.labels == ["agent-1: → Read a.py ✓ 1 line"]


def test_a_tick_within_a_frame_leaves_the_spinner_as_it_is() -> None:
    session = Session()
    session.events(call("agent-1", "c1"))
    before = session.shown.rows[0]
    session.conversation.tick()
    assert session.shown.rows[0] is before


def ask(agent_id: str, request_id: str = "r") -> AgentRequest:
    return AgentRequest(
        agent_id=agent_id,
        request_id=request_id,
        tool="complete_task",
        arguments={"summary": "Add a flag"},
    )


def answer(agent_id: str, text: str, request_id: str = "r", is_error: bool = False) -> AgentReply:
    return AgentReply(agent_id=agent_id, request_id=request_id, text=text, is_error=is_error)


ASKED = "agent-1 → orbit complete_task Add a flag"
"""How agent-1's request to orbit reads in its entry."""


def test_a_request_to_orbit_and_its_reply_are_one_entry_updated_in_place() -> None:
    session = Session()
    session.events(ask("agent-1"))
    assert session.shown.labels == [f"◜ {ASKED}"] and session.window.ticking
    session.events(answer("agent-1", "Checks passed.\nMerged."))
    assert session.shown.labels == [f"{ASKED} ✓ Checks passed."]
    assert not session.window.ticking
    assert session.shown.rows[0].component == "transcript--request"


def test_a_refused_request_is_crossed_in_the_error_style() -> None:
    session = Session()
    session.events(ask("agent-1"), answer("agent-1", "No.", is_error=True))
    assert session.shown.labels == [f"{ASKED} ✗ No."]
    assert session.shown.rows[0].component == "transcript--error"


def test_a_long_reply_is_clipped_to_one_line() -> None:
    session = Session()
    session.events(ask("agent-1"), answer("agent-1", "a" * 200))
    assert session.shown.labels == [f"{ASKED} ✓ {'a' * (REPLY_LIMIT - 1)}…"]


def test_a_requests_entry_keeps_its_full_arguments_and_reply() -> None:
    session = Session()
    session.events(ask("agent-1"), answer("agent-1", "x\ny"))
    kept = session.conversation.entries(None)[0].call
    assert kept is not None
    assert (kept.arguments, kept.output) == ({"summary": "Add a flag"}, "x\ny")


def test_a_reply_to_an_unknown_request_is_an_entry_of_its_own() -> None:
    session = Session()
    session.events(ask("agent-1"), answer("agent-1", "ok", "other"))
    assert session.shown.labels == [f"◜ {ASKED}", "orbit → agent-1: ok"]


def test_a_reply_doesnt_finish_a_tool_call_of_the_same_id() -> None:
    session = Session()
    session.events(call("agent-1", "r"), answer("agent-1", "ok"))
    assert session.shown.labels == ["agent-1: ◜ Read a.py", "orbit → agent-1: ok"]


def test_a_request_keeps_waiting_when_its_turn_ends() -> None:
    session = Session()
    session.events(ask("agent-1"), turn_ended("agent-1"))
    assert session.shown.labels == [f"◜ {ASKED}", "agent-1 done"]


def test_a_request_unanswered_when_its_agent_exits_has_no_reply() -> None:
    session = Session()
    session.events(
        ask("agent-1"), ask("agent-2"), AgentExited(agent_id="agent-1", reason="crashed")
    )
    assert session.shown.labels[:2] == [
        f"{ASKED} ⊘ no reply",
        "◜ agent-2 → orbit complete_task Add a flag",
    ]
    assert session.shown.rows[0].component == "transcript--tool"
    assert session.window.ticking


def test_a_request_updates_under_all_and_its_roles_tab() -> None:
    session = Session()
    session.events(started("agent-1"), ask("agent-1"), answer("agent-1", "ok"))
    assert session.shown.labels[1] == f"{ASKED} ✓ ok"
    session.show_only("coder", "coder")
    assert session.shown.labels == ["▶ agent-1 (coder) started", f"{ASKED} ✓ ok"]


def test_nothing_ticks_while_nothing_runs() -> None:
    session = Session()
    session.events(started("agent-2"))
    assert not session.window.ticking and session.window.row.shows == []


def test_the_working_row_ticks_while_the_planners_turn_runs() -> None:
    session = Session()
    session.events(started("agent-1", "planner"), turn_began("agent-1"))
    assert lines(session) == ("◜ planner · 0:00",) and session.window.ticking
    session.events(turn_ended("agent-1"))
    assert lines(session) == () and not session.window.ticking


def test_one_ticker_turns_a_call_and_its_row_until_the_turn_ends() -> None:
    session = Session()
    session.window.now = 222.0
    session.events(started("agent-2"), turn_began("agent-2"), call("agent-2", "c1", "a"))
    assert session.window.ticking
    session.window.now = 234.0 + 1 / SPINNER_FPS
    session.conversation.tick()
    assert lines(session) == ("◠ agent-2 · Read a 0:12 · 0:12",)
    assert session.shown.labels[-1] == "agent-2: ◠ Read a"
    session.events(turn_ended("agent-2"))
    assert lines(session) == () and not session.window.ticking


def test_an_agent_exiting_leaves_the_working_row_and_stops_the_ticks() -> None:
    session = Session()
    session.events(started("agent-2"), turn_began("agent-2"))
    session.events(AgentExited(agent_id="agent-2", reason="crashed"))
    assert lines(session) == () and not session.window.ticking


def test_each_tab_shows_only_its_own_running_agents_while_all_still_tick() -> None:
    session = Session()
    session.events(started("agent-1", "planner"), started("agent-2"))
    session.events(turn_began("agent-1"), turn_began("agent-2"))
    assert lines(session) == ("◜ planner · 0:00", "◜ agent-2 · 0:00")
    session.show_only("coder", "coder")
    assert lines(session) == ("◜ agent-2 · 0:00",)
    session.show_only(PLANNER, PLANNER)
    assert lines(session) == ("◜ planner · 0:00",)
    session.show_only("reviewer", "reviewer")
    assert lines(session) == ()
    assert session.window.ticking  # Others still run.


def test_a_cancelled_planner_turn_leaves_the_row_and_ends_its_running_call() -> None:
    session = Session()
    session.events(started("agent-1", "planner"), turn_began("agent-1"), call("agent-1", "c1"))
    assert lines(session) == ("◜ planner · Read a.py 0:00 · 0:00",)
    session.events(TurnFinished(turn_id="t", agent_id="agent-1", reason="cancelled"))
    assert lines(session) == ()
    assert "agent-1: → Read a.py ⊘ no result" in session.shown.labels


def snapshotted(session: Session, snapshot: Snapshot) -> None:
    """Have ``session`` replace its conversation with ``snapshot``'s, its working row following
    the snapshot once its roles are known, as the app does."""
    session.feed.replace((), snapshot.agents)
    session.conversation.track(snapshot, session.tabs.roles)


def planning(agent: str, busy: bool) -> Snapshot:
    """A snapshot of the old session, its planner ``agent`` live and mid-turn if ``busy``."""
    planner = RunningAgent(agent_id=agent, role="planner", task_id="")
    return snapshot_of(OLD).model_copy(update={"agents": (planner,), "busy": {PLANNER_LANE: busy}})


def test_a_snapshot_with_an_idle_planner_shows_no_planner_line() -> None:
    session = Session()
    snapshotted(session, planning("agent-1", busy=False))
    assert lines(session) == () and not session.window.ticking


def test_a_snapshot_shows_its_busy_planner_as_the_planner() -> None:
    session = Session()
    session.events(started("agent-1", "planner"))
    snapshotted(session, planning("agent-7", busy=True))
    assert lines(session) == ("◜ planner · 0:00",)
    session.show_only(PLANNER, PLANNER)
    assert lines(session) == ("◜ planner · 0:00",) and session.window.ticking
    session.events(turn_ended("agent-7"))
    assert lines(session) == () and not session.window.ticking


def test_a_snapshot_clears_the_working_lines_and_the_ticks() -> None:
    session = Session()
    session.events(started("agent-2"), turn_began("agent-2"), call("agent-2", "c1", "a"))
    snapshotted(session, snapshot_of(OLD))
    assert lines(session) == () and not session.window.ticking
    assert session.conversation.entries(None) == ()
