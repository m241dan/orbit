"""y copies the whole of the open detail pane's call to the clipboard and says how much."""

import pytest
from textual.pilot import Pilot

from orbit.protocol import ToolCallFinished, ToolCallStarted
from orbit.tui import OrbitApp
from tests.tui_fakes import FakeSession, feed_events, said

LONG = "\n".join(f"line {n}" for n in range(250))


def read(call_id: str) -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", call_id=call_id, name="Read", arguments={"file_path": "a.py"}, agent_id="a"
    )


def done(call_id: str, output: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", call_id=call_id, output=output, agent_id="a")


def notes(app: OrbitApp, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The toasts ``app`` shows from now on."""
    shown: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **_: shown.append(message))
    return shown


async def inspecting(pilot: Pilot[None]) -> None:
    """Activate the conversation and open the details of its latest entry."""
    await pilot.press("k", "enter", "enter")


async def test_y_copies_the_whole_output_including_lines_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    toasts = notes(app, monkeypatch)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", LONG))
        await inspecting(pilot)
        await pilot.press("y")
        assert app.clipboard == LONG and toasts == ["Copied 250 lines"]


async def test_y_copies_from_the_activated_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    toasts = notes(app, monkeypatch)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await inspecting(pilot)
        await pilot.press("tab", "y")
        assert app.clipboard == "x" and toasts == ["Copied 1 line"]


async def test_y_copies_a_running_calls_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    toasts = notes(app, monkeypatch)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"))
        await inspecting(pilot)
        await pilot.press("y")
        assert app.clipboard == '{\n  "file_path": "a.py"\n}' and toasts == ["Copied 3 lines"]


async def test_y_does_nothing_on_an_entry_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    toasts = notes(app, monkeypatch)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, said("a", "hi"), read("c1"), done("c1", "x"))
        await inspecting(pilot)
        await pilot.press("k", "y")
        assert app.clipboard == "" and toasts == []


async def test_y_does_nothing_while_the_pane_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    toasts = notes(app, monkeypatch)
    async with app.run_test(size=(80, 30)) as pilot:
        await feed_events(session, pilot, read("c1"), done("c1", "x"))
        await pilot.press("k", "enter", "y")
        assert app.clipboard == "" and toasts == []
