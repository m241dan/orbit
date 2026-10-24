"""The detail pane's content, without the pane showing it: which call it shows, while it's
open, and when it starts again at the top."""

from orbit.protocol import ToolCallFinished, ToolCallStarted
from orbit.tui.view.detail import NO_DETAILS, WAITING
from orbit.tui.view.detail_views import Detail
from orbit.tui.view.tool_calls import ToolCall, tool_call_finished, tool_call_started
from tests.transcript_fakes import Pane

RULE = "─" * 40


def read(call_id: str, path: str = "a.py") -> ToolCall:
    return tool_call_started(
        ToolCallStarted(
            turn_id="t",
            call_id=call_id,
            name="Read",
            arguments={"file_path": path},
            agent_id="agent-1",
        )
    )


def done(call: ToolCall, output: str) -> ToolCall:
    finished = ToolCallFinished(
        turn_id="t", call_id=call.call_id, output=output, agent_id="agent-1"
    )
    return tool_call_finished(call, finished)


def opened() -> tuple[Detail, Pane]:
    pane = Pane()
    return Detail(pane), pane


def test_the_pane_starts_closed_showing_nothing() -> None:
    detail, pane = opened()
    assert not detail.opened and detail.call is None
    assert not pane.open and pane.tops == 0


def test_showing_a_call_opens_its_details_at_the_top() -> None:
    detail, pane = opened()
    call = done(read("c1"), "one\ntwo")
    detail.show(call)
    assert detail.opened and detail.call == call
    assert pane.open and pane.title == "Detail · Read a.py · y copy"
    assert pane.body == ["a.py", RULE, "one", "two"]
    assert pane.tops == 1


def test_an_entry_without_details_shows_a_note() -> None:
    detail, pane = opened()
    detail.show(None)
    assert detail.opened and pane.title == "Detail" and pane.body == [NO_DETAILS]


def test_a_running_call_waits_then_shows_its_result_in_place() -> None:
    detail, pane = opened()
    running = read("c1")
    detail.show(running)
    assert pane.body == ["a.py", RULE, WAITING]
    detail.show(done(running, "done"))
    assert pane.body == ["a.py", RULE, "done"]
    assert pane.tops == 1  # The same call updated keeps its place.


def test_another_call_starts_at_the_top() -> None:
    detail, pane = opened()
    detail.show(read("c1"))
    detail.show(read("c2", "b.py"))
    assert pane.title == "Detail · Read b.py · y copy" and pane.tops == 2


def test_an_entry_without_details_after_a_call_starts_at_the_top_and_back_again() -> None:
    detail, pane = opened()
    detail.show(read("c1"))
    detail.show(None)
    assert pane.tops == 2
    detail.show(None)
    assert pane.tops == 2
    detail.show(read("c1"))
    assert pane.tops == 3


def test_closing_hides_the_pane_and_reopening_starts_at_the_top() -> None:
    detail, pane = opened()
    call = read("c1")
    detail.show(call)
    detail.close()
    assert not detail.opened and not pane.open
    detail.show(call)
    assert detail.opened and pane.open and pane.tops == 2
