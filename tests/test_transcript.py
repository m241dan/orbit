"""The transcript keeps entries per tab, each tab a list of entries that can be moved between
and collapsed."""

from rich.text import Text
from textual.pilot import Pilot

from orbit.protocol import AssistantMessage, Notice, ToolCallFinished
from orbit.tui import OrbitApp
from orbit.tui.widgets import Transcript, TranscriptLog
from tests.test_tui import FakeSession, feed_events, said, started

LONG = " ".join(["word"] * 30)  # Wraps in any window a test opens.


def visible_log(app: OrbitApp) -> TranscriptLog:
    return app.query_one("Transcript > TranscriptLog.-shown", TranscriptLog)


def prompts(app: OrbitApp) -> list[Text]:
    log = visible_log(app)
    texts = []
    for index in range(log.option_count):
        prompt = log.get_option_at_index(index).prompt
        assert isinstance(prompt, Text)
        texts.append(prompt)
    return texts


def labels(app: OrbitApp) -> list[str]:
    """The shown log's entries as they're labelled."""
    return [prompt.plain.removesuffix("\n") for prompt in prompts(app)]


async def fed(session: FakeSession, pilot: Pilot[None], app: OrbitApp, count: int) -> None:
    """Feed ``count`` lines and wait until the shown log has them all."""
    for i in range(count):
        await session.queue.put(said("agent-1", f"line {i}"))
    for _ in range(100):
        await pilot.pause()
        if visible_log(app).option_count == count:
            return
    raise AssertionError(f"only {visible_log(app).option_count} of {count} entries arrived")


def tool_output(agent_id: str, output: str) -> ToolCallFinished:
    return ToolCallFinished(turn_id="t", call_id="c", output=output, agent_id=agent_id)


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


async def test_entries_are_shown_as_paragraphs_separated_by_blank_lines() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi\nthere"))
        assert labels(app) == ["▶ agent-1 (coder) started", "▾ agent-1: hi\nthere"]
        assert all(prompt.plain.endswith("\n") for prompt in prompts(app))
        log = visible_log(app)
        rendered = [log.render_line(y).text.strip() for y in range(5)]
        assert rendered == ["▶ agent-1 (coder) started", "", "▾ agent-1: hi", "there", ""]


async def test_entry_kinds_style_the_entries() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        await feed_events(session, pilot, AssistantMessage(turn_id="t", text="plain"))
        user, plain = prompts(app)
        assert user.style == visible_log(app).get_component_rich_style("transcript--user")
        assert isinstance(user.style, str) or user.style.bold
        assert plain.style == visible_log(app).get_component_rich_style("transcript--assistant")


async def test_tool_output_of_several_lines_starts_collapsed() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            started("agent-1"),
            tool_output("agent-1", "one line"),
            tool_output("agent-1", "first\nsecond\nthird"),
            tool_output("agent-1", "first\nsecond"),
        )
        assert labels(app)[1:] == [
            "agent-1: one line",
            "▸ agent-1: first (+2 lines)",
            "▸ agent-1: first (+1 line)",
        ]
        log = visible_log(app)
        assert [log.collapsed(i) for i in range(4)] == [False, False, True, True]


async def test_the_conversation_can_be_activated_to_move_between_entries_and_toggle_them() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session, pilot, started("agent-1"), tool_output("agent-1", "first\nsecond")
        )
        await pilot.press("k", "enter")  # Up to the conversation, then activate it.
        log = visible_log(app)
        assert app.focused is log and log.highlighted == 1  # The latest entry.
        await pilot.press("enter")
        assert labels(app)[1] == "▾ agent-1: first\nsecond"
        await pilot.press("enter")
        assert labels(app)[1] == "▸ agent-1: first (+1 line)"
        await pilot.press("k", "enter")  # A one-line entry has nothing to collapse.
        assert log.highlighted == 0 and labels(app)[0] == "▶ agent-1 (coder) started"
        await pilot.press("escape")
        assert app.focused is None
        assert not log.can_focus
        await pilot.press("enter")  # Activated again, it picks up where it was.
        assert app.focused is log and log.highlighted == 0


async def test_the_shown_log_stays_pinned_to_the_latest_entry_unless_active() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        log = visible_log(app)
        assert log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y
        await pilot.press("k", "enter")  # Active, at the latest entry.
        assert log.highlighted == 39
        await pilot.press("home")
        assert log.highlighted == 0 and log.scroll_y == 0
        await feed_events(session, pilot, said("agent-1", "later"))
        assert log.scroll_y == 0 and log.highlighted == 0
        await pilot.press("escape")
        await feed_events(session, pilot, said("agent-1", "latest"))
        assert log.scroll_y == log.max_scroll_y


async def test_switching_tabs_leaves_list_mode_and_shows_the_tabs_end() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        await pilot.press("k", "enter", "home")
        await pilot.press("ctrl+t")  # The planner tab.
        assert app.focused is None
        await pilot.press("ctrl+t")  # Back to All, whose log is laid out again at its end.
        log = visible_log(app)
        assert log.scroll_y == log.max_scroll_y > 0


async def test_a_tab_shows_only_its_entries_and_all_shows_every_one() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "coding"))
        await feed_events(
            session, pilot, started("agent-2", "planner"), said("agent-2", "planning")
        )
        assert len(app.query(TranscriptLog)) == 3  # All, and a log per tab with entries.
        await pilot.press("tab")  # planner
        assert app.query_one(Transcript).only == "planner"
        assert labels(app) == ["▶ agent-2 (planner) started", "agent-2: planning"]
        await pilot.press("tab")  # coder
        assert labels(app) == ["▶ agent-1 (coder) started", "agent-1: coding"]
        await pilot.press("tab")  # reviewer: no entries, so its log is built now
        assert labels(app) == []
        assert len(app.query(TranscriptLog)) == 4
        await pilot.press(*["tab"] * 3)  # back round to All
        assert app.query_one(Transcript).only is None
        assert len(labels(app)) == 4


async def test_output_for_a_tab_not_shown_is_there_when_it_is() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "first"))
        await pilot.press("tab", "tab")  # coder: its log is built and shown
        assert labels(app) == ["▶ agent-1 (coder) started", "agent-1: first"]
        await pilot.press("tab", "tab", "tab", "tab")  # All: coder's log is hidden
        await feed_events(session, pilot, said("agent-1", "second"))
        await pilot.press("tab", "tab")
        assert labels(app)[-1] == "agent-1: second"


async def test_lines_are_wrapped_again_for_a_new_window_size() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(60, 24)) as pilot:
        await feed_events(session, pilot, *(said("agent-1", LONG) for _ in range(6)))
        narrow = visible_log(app).max_scroll_y
        await pilot.resize_terminal(160, 24)
        await pilot.pause()
        assert visible_log(app).max_scroll_y < narrow
