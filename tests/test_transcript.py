"""The transcript keeps entries per tab and shows each tab through its own log."""

from rich.style import Style
from textual.strip import Strip
from textual.widgets import RichLog

from orbit.protocol import AssistantMessage, Notice
from orbit.tui import OrbitApp
from orbit.tui.widgets import Transcript
from tests.test_tui import FakeSession, feed_events, said, started

LONG = " ".join(["word"] * 30)  # Wraps in any window a test opens.


def visible_log(app: OrbitApp) -> RichLog:
    return app.query_one("Transcript > RichLog.-shown", RichLog)


def lines(app: OrbitApp) -> list[str]:
    """The shown log's lines, as text."""
    return [strip.text.rstrip() for strip in visible_log(app).lines]


def text_lines(app: OrbitApp) -> list[str]:
    return [line for line in lines(app) if line]


def style_of(strip: Strip) -> Style:
    style = next(iter(strip)).style
    assert style is not None
    return style


async def test_entries_are_kept_under_all_and_under_their_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session, pilot, started("agent-1"), said("agent-1", "hi"), Notice(message="n")
        )
        transcript = app.query_one(Transcript)
        assert transcript.entries(None) == (
            ("▶ agent-1 (coder) started", "agent"),
            ("agent-1: hi", "assistant"),
            ("📌 n", "verify"),
        )
        # agent-1 runs in a pinned role, so its entries belong to the role's tab.
        assert transcript.entries("coder") == transcript.entries(None)[:2]
        assert transcript.entries("agent-1") == ()


async def test_the_shown_log_separates_entries_with_blank_lines() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi\nthere"))
        assert lines(app) == ["▶ agent-1 (coder) started", "", "agent-1: hi", "there", ""]


async def test_entry_kinds_style_the_lines() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        await feed_events(session, pilot, AssistantMessage(turn_id="t", text="plain"))
        user, _, plain, _ = visible_log(app).lines
        transcript = app.query_one(Transcript)
        assert style_of(user) == transcript.get_component_rich_style("transcript--user")
        assert style_of(user).bold
        assert not style_of(plain).bold


async def test_a_tab_shows_only_its_entries_and_all_shows_every_one() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "coding"))
        await feed_events(
            session, pilot, started("agent-2", "planner"), said("agent-2", "planning")
        )
        assert len(app.query(RichLog)) == 3  # All, and a log per tab with entries.
        await pilot.press("tab")  # planner
        assert app.query_one(Transcript).only == "planner"
        assert text_lines(app) == ["▶ agent-2 (planner) started", "agent-2: planning"]
        await pilot.press("tab")  # coder
        assert text_lines(app) == ["▶ agent-1 (coder) started", "agent-1: coding"]
        await pilot.press("tab")  # reviewer: no entries, so its log is built now
        assert text_lines(app) == []
        assert len(app.query(RichLog)) == 4
        await pilot.press(*["tab"] * 3)  # back round to All
        assert app.query_one(Transcript).only is None
        assert len(text_lines(app)) == 4


async def test_output_for_a_tab_not_shown_is_there_when_it_is() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "first"))
        await pilot.press("tab", "tab")  # coder: its log is built and shown
        assert text_lines(app) == ["▶ agent-1 (coder) started", "agent-1: first"]
        await pilot.press("tab", "tab", "tab", "tab")  # All: coder's log is hidden
        await feed_events(session, pilot, said("agent-1", "second"))
        await pilot.press("tab", "tab")
        assert text_lines(app)[-1] == "agent-1: second"


async def test_lines_are_wrapped_again_for_a_new_window_size() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(60, 24)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", LONG))
        await pilot.press("tab", "tab")  # coder: built narrow
        narrow = len(text_lines(app))
        await pilot.press(*["tab"] * 4)  # All
        assert len(text_lines(app)) == narrow
        await pilot.resize_terminal(160, 24)
        await pilot.pause()
        wide = len(text_lines(app))
        assert wide < narrow
        # A log that wasn't shown during the resize is wrapped again when shown, with what
        # arrived for it meanwhile in place once.
        await feed_events(session, pilot, said("agent-1", "later"))
        await pilot.press("tab", "tab")
        assert len(text_lines(app)) == wide + 1
        assert text_lines(app)[-1] == "agent-1: later"
