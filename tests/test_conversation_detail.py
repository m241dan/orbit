"""The detail pane on the conversation's cursor, without the window showing it: enter on a tool
call or request opens its full content, which follows the cursor until escape closes it, and
y copies it."""

from orbit.protocol import AgentReply, AgentRequest, ToolCallFinished, ToolCallStarted
from orbit.tui.view.detail import DETAIL_COVER_BELOW, NO_DETAILS, WAITING
from tests.transcript_fakes import Session
from tests.tui_fakes import said, started

RULE = "─" * 40
LONG = "\n".join(f"line {n}" for n in range(250))


def read(call_id: str, path: str = "a.py") -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", call_id=call_id, name="Read", arguments={"file_path": path}, agent_id="agent-1"
    )


def done(call_id: str, output: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", call_id=call_id, output=output, agent_id="agent-1")


def inspecting(session: Session) -> None:
    """Activate the conversation and press enter on its latest entry."""
    session.activate()
    session.choose()


def test_enter_on_a_tool_call_opens_its_details() -> None:
    session = Session()
    session.events(read("c1"), done("c1", "one\ntwo"))
    pane = session.window.pane
    assert not session.window.detail.opened and not pane.open
    inspecting(session)
    assert session.window.detail.opened and pane.title == "Detail · Read a.py · y copy"
    assert pane.body == ["a.py", RULE, "one", "two"]
    assert session.shown.has_focus and not session.window.covering


def test_enter_on_a_request_opens_its_arguments_and_reply() -> None:
    session = Session()
    request = AgentRequest(
        agent_id="agent-1", request_id="r", tool="complete_task", arguments={"summary": "Go"}
    )
    session.events(request, AgentReply(agent_id="agent-1", request_id="r", text="Merged."))
    inspecting(session)
    pane = session.window.pane
    assert pane.title == "Detail · complete_task Go · y copy"
    assert pane.body == ["{", '  "summary": "Go"', "}", RULE, "orbit: Merged."]


def test_enter_on_a_message_still_collapses_and_expands_it() -> None:
    session = Session()
    session.events(started("agent-1"), said("agent-1", "a\nb"))
    inspecting(session)
    assert session.shown.entry_log.collapsed(1) and not session.window.detail.opened


def test_the_pane_follows_the_cursor_and_notes_entries_without_details() -> None:
    session = Session()
    session.events(said("agent-1", "hi"), read("c1"), done("c1", "x"), read("c2", "b.py"))
    inspecting(session)
    pane = session.window.pane
    assert pane.title == "Detail · Read b.py · y copy"
    session.move(1)
    assert pane.title == "Detail · Read a.py · y copy" and pane.body == ["a.py", RULE, "x"]
    assert pane.tops == 2  # Another entry starts at the top.
    session.move(0)
    assert pane.open and pane.title == "Detail" and pane.body == [NO_DETAILS]


def test_a_running_call_waits_then_shows_its_result_in_place() -> None:
    session = Session()
    session.events(read("c1"))
    inspecting(session)
    pane = session.window.pane
    assert pane.body == ["a.py", RULE, WAITING]
    session.events(done("c1", "done"))
    assert pane.body == ["a.py", RULE, "done"] and pane.tops == 1


def test_another_call_finishing_leaves_the_open_pane_alone() -> None:
    session = Session()
    session.events(read("c2", "b.py"), read("c1"), done("c1", LONG))
    inspecting(session)
    pane = session.window.pane
    before, tops = pane.body, pane.tops
    pane.body = []
    session.events(done("c2", "x"))
    assert pane.body == [] and pane.tops == tops and before[0] == "a.py"


def test_escape_closes_the_pane_first_then_leaves_list_mode() -> None:
    session = Session()
    session.events(read("c1"), done("c1", "x"))
    inspecting(session)
    log = session.shown
    session.escape()
    assert not session.window.detail.opened and log.has_focus
    session.escape()
    assert not log.has_focus and log.highlighted == 0


def test_leaving_the_conversation_closes_the_pane_and_letting_go_keeps_it_closed() -> None:
    session = Session()
    session.events(read("c1"), done("c1", "x"))
    inspecting(session)
    log = session.shown
    log.has_focus = False
    log.entry_log.blurred(False)
    session.window.settle()
    assert not session.window.detail.opened and not log.entry_log.inspecting
    session.release()
    assert not session.window.detail.opened


def test_the_pane_stays_open_while_the_detail_pane_takes_the_focus() -> None:
    session = Session()
    session.events(read("c1"), done("c1", "x"))
    inspecting(session)
    session.shown.entry_log.blurred(True)
    session.window.settle()
    assert session.window.detail.opened and session.shown.entry_log.inspecting


def test_a_closed_pane_stays_closed_as_the_cursor_moves() -> None:
    session = Session()
    session.events(read("c1"), done("c1", "x"), read("c2"))
    inspecting(session)
    session.escape()
    session.move(0)
    session.events(done("c2", "y"))
    assert not session.window.detail.opened


def test_switching_tabs_closes_the_pane_for_good() -> None:
    session = Session()
    session.events(read("c1"), done("c1", "x"))
    inspecting(session)
    session.show_only("planner", "planner")
    assert not session.window.detail.opened
    session.show_only(None)
    session.activate()
    session.move(0)
    assert not session.window.detail.opened


def test_letting_go_of_the_held_entry_closes_the_pane() -> None:
    session = Session(height=DETAIL_COVER_BELOW - 1)
    session.events(read("c1"), done("c1", LONG))
    inspecting(session)
    assert session.window.covering
    session.release()
    log = session.shown
    assert not session.window.detail.opened and not session.window.covering
    assert log.highlighted is None and not log.entry_log.inspecting


def test_on_a_short_conversation_the_open_pane_covers_it_until_it_closes() -> None:
    session = Session(height=DETAIL_COVER_BELOW - 1)
    session.events(read("c1"), done("c1", "x"))
    inspecting(session)
    assert session.window.covering
    session.escape()
    assert not session.window.covering


def test_a_conversation_grown_tall_enough_gives_back_its_room() -> None:
    session = Session(height=DETAIL_COVER_BELOW - 1)
    session.events(read("c1"), done("c1", "x"))
    inspecting(session)
    session.window.content_height = DETAIL_COVER_BELOW
    session.conversation.fit_detail()
    assert not session.window.covering
    session.window.content_height = DETAIL_COVER_BELOW - 1
    session.conversation.fit_detail()
    assert session.window.covering


def test_reopening_the_pane_starts_at_the_top() -> None:
    session = Session()
    session.events(read("c1"), done("c1", LONG))
    inspecting(session)
    session.escape()
    session.choose()
    assert session.window.detail.opened and session.window.pane.tops == 2


def test_clearing_the_conversation_closes_the_pane_and_drops_every_tabs_log() -> None:
    session = Session()
    session.events(started("agent-1"), read("c1"), done("c1", "x"))
    session.show_only("coder", "coder")
    coder = session.shown
    inspecting(session)
    session.conversation.clear()
    window = session.window
    assert not window.detail.opened and coder.rows == [] and not coder.removed
    assert [log.removed for log in window.logs] == [True, False]
    assert session.conversation.entries(None) == ()


def test_y_copies_the_whole_output_including_lines_omitted() -> None:
    session = Session()
    session.events(read("c1"), done("c1", LONG))
    inspecting(session)
    session.conversation.copy_detail()
    assert session.window.copies == [(LONG, "Copied 250 lines")]


def test_y_copies_a_running_calls_arguments() -> None:
    session = Session()
    session.events(read("c1"))
    inspecting(session)
    session.conversation.copy_detail()
    assert session.window.copies == [('{\n  "file_path": "a.py"\n}', "Copied 3 lines")]


def test_y_copies_nothing_from_an_entry_without_details() -> None:
    session = Session()
    session.events(said("a", "hi"), read("c1"), done("c1", "x"))
    inspecting(session)
    session.move(0)
    session.conversation.copy_detail()
    assert session.window.copies == []
