"""The transcript keeps entries per tab, each tab a list of entries that can be moved between
and collapsed."""

import pytest
from rich.color import Color
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.pilot import Pilot

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AssistantMessage,
    ErrorOccurred,
    Event,
    Notice,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
    Usage,
)
from orbit.tui import OrbitApp
from orbit.tui.transcript import (
    Entry,
    GutteredEntry,
    HangingIndent,
    Transcript,
    TranscriptLog,
    entry_markdown,
)
from orbit.tui.view import (
    GUTTER_BAR,
    GUTTER_MARKER,
    PROMPT_LINES_SHOWN,
    REPLY_LIMIT,
    SPINNER_FPS,
    EntryKind,
)
from orbit.tui.widgets import ChatPrompt
from tests.test_tui import FakeSession, feed_events, said, started

# Wraps in any window a test opens. At 40 columns a row beside the gutter holds two words but
# would hold three if wrapped at the full width, so cropping a row would lose text.
LONG = " ".join(["gutter"] * 20)


def visible_log(app: OrbitApp) -> TranscriptLog:
    return app.query_one("Transcript > TranscriptLog.-shown", TranscriptLog)


def entry(log: TranscriptLog, index: int) -> GutteredEntry:
    """The entry at ``index`` as the log draws it."""
    prompt = log.get_option_at_index(index).prompt
    assert isinstance(prompt, GutteredEntry)
    return prompt


def prompts(app: OrbitApp) -> list[GutteredEntry]:
    log = visible_log(app)
    return [entry(log, index) for index in range(log.option_count)]


def labels(app: OrbitApp) -> list[str]:
    """The shown log's entries as they're labelled, without their gutter."""
    return [prompt.label.plain for prompt in prompts(app)]


def gutters(app: OrbitApp) -> list[str]:
    """The gutter symbol of each of the shown log's entries."""
    return [prompt.gutter.strip() for prompt in prompts(app)]


def label_style(prompt: GutteredEntry) -> Style:
    """The style of an entry's label."""
    return prompt.label.get_style_at_offset(Console(), 0)


def rendered(log: TranscriptLog, rows: int) -> list[str]:
    """The text of the log's first ``rows`` rows as drawn."""
    return [log.render_line(y).text.rstrip() for y in range(rows)]


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
            Entry("▶ agent-1 (coder) started", "agent", "agent-1", key="coder"),
            Entry("hi", "assistant", "agent-1", "t", key="coder"),
            Entry("📌 n", "verify"),
        )
        # agent-1 runs in a pinned role, so its entries belong to the role's tab.
        assert transcript.entries("coder") == transcript.entries(None)[:2]
        assert transcript.entries("agent-1") == ()


async def test_entries_are_shown_as_paragraphs_separated_by_blank_lines() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi\n\nthere"))
        assert labels(app) == ["▶ agent-1 (coder) started", "▾ hi\n\nthere"]
        assert rendered(visible_log(app), 6) == [
            "│ ▶ agent-1 (coder) started",
            "",
            "│ ▾ hi",
            "│",
            "│   there",
            "",
        ]


async def test_entry_kinds_style_the_entries() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry("Task 1: do it", "user"))
        log.append(Entry("plain", "assistant"))
        await pilot.pause()
        header, plain = (label_style(entry(log, index)) for index in range(2))
        assert header == log.get_component_rich_style("transcript--user", partial=True)
        assert isinstance(header, Style) and header.bold
        assert plain == log.get_component_rich_style("transcript--assistant", partial=True)
        assert header.bgcolor is None


async def test_every_entry_has_a_dim_bar_in_its_gutter_without_a_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi\nthere"))
        assert visible_log(app).highlighted is None
        assert gutters(app) == [GUTTER_BAR, GUTTER_BAR]
        assert prompts(app)[1].gutter_style == visible_log(app).get_component_rich_style(
            "transcript--gutter", partial=True
        )


def assert_rows_hold_label(log: TranscriptLog, rows: int, label: str) -> None:
    """Assert the log's first ``rows`` rows fit its content width and, past their gutter, hold
    all of ``label``, so wrapping beside the gutter crops nothing."""
    lines = rendered(log, rows)
    assert all(len(line) <= log.scrollable_content_region.width for line in lines)
    assert " ".join(line[2:].rstrip() for line in lines) == label


async def test_every_wrapped_row_of_an_entry_keeps_its_gutter() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(40, 24)) as pilot:
        await feed_events(session, pilot, AssistantMessage(turn_id="t", text=LONG))
        log = visible_log(app)
        rows = log.virtual_size.height
        assert rows > 3  # The entry wraps onto several rows, then a blank one.
        assert all(row.startswith(f"{GUTTER_BAR} ") for row in rendered(log, rows - 1))
        assert rendered(log, rows)[-1] == ""
        assert_rows_hold_label(log, rows - 1, labels(app)[0])
        await pilot.press("k", "enter")
        assert all(row.startswith(f"{GUTTER_MARKER} ") for row in rendered(log, rows - 1))
        assert rendered(log, rows)[-1] == ""
        assert_rows_hold_label(log, rows - 1, labels(app)[0])


async def test_the_cursors_entry_is_marked_and_the_marker_moves_with_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi\nthere"))
        await pilot.press("k", "enter")
        assert gutters(app) == [GUTTER_BAR, GUTTER_MARKER]
        assert prompts(app)[1].gutter_style == visible_log(app).get_component_rich_style(
            "transcript--marker", partial=True
        )
        await pilot.press("k")
        assert gutters(app) == [GUTTER_MARKER, GUTTER_BAR]
        await pilot.press("j")
        assert gutters(app) == [GUTTER_BAR, GUTTER_MARKER]


async def test_a_toggled_entry_keeps_its_marker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), tool_output("agent-1", "a\nb"))
        await pilot.press("k", "enter", "enter")
        assert labels(app)[1] == "▾ agent-1: a\nb"
        assert gutters(app) == [GUTTER_BAR, GUTTER_MARKER]


async def test_the_marker_stays_after_leaving_and_goes_with_the_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi"))
        await pilot.press("k", "enter", "escape")
        log = visible_log(app)
        assert app.focused is None and log.highlighted == 1
        assert gutters(app) == [GUTTER_BAR, GUTTER_MARKER]
        log.highlighted = None
        await pilot.pause()
        assert gutters(app) == [GUTTER_BAR, GUTTER_BAR]


async def test_the_cursors_entry_has_no_highlight_background() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi"))
        await pilot.press("k", "enter")
        log = visible_log(app)
        plain = log.get_visual_style("option-list--option")
        for focused in (True, False):
            assert (app.focused is log) == focused
            for state in ("option-list--option-highlighted", "option-list--option-hover"):
                assert log.get_visual_style("option-list--option", state) == plain
            await pilot.press("escape")


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


async def test_the_shown_log_stays_pinned_to_the_latest_entry_unless_it_has_a_cursor() -> None:
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
        await pilot.press("escape", "escape")  # Leave, then let go of the cursor.
        await feed_events(session, pilot, said("agent-1", "latest"))
        assert log.scroll_y == log.max_scroll_y


async def test_switching_tabs_shows_the_end_of_a_tab_with_no_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        await pilot.press("ctrl+t")  # The planner tab.
        await pilot.press("ctrl+t")  # Back to All, whose log is laid out again at its end.
        log = visible_log(app)
        assert await scrolled_to_end(pilot, log)
        assert log.max_scroll_y > 0


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
        assert labels(app) == ["▶ agent-2 (planner) started", "planning"]
        await pilot.press("tab")  # coder
        assert labels(app) == ["▶ agent-1 (coder) started", "coding"]
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
        assert labels(app) == ["▶ agent-1 (coder) started", "first"]
        await pilot.press("tab", "tab", "tab", "tab")  # All: coder's log is hidden
        await feed_events(session, pilot, said("agent-1", "second"))
        await pilot.press("tab", "tab")
        assert labels(app)[-1] == "second"


async def test_lines_are_wrapped_again_for_a_new_window_size() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(60, 24)) as pilot:
        await feed_events(session, pilot, *(said("agent-1", LONG) for _ in range(6)))
        narrow = visible_log(app).max_scroll_y
        await pilot.resize_terminal(160, 24)
        await pilot.pause()
        assert visible_log(app).max_scroll_y < narrow


async def scrolled_to_end(pilot: Pilot[None], log: TranscriptLog) -> bool:
    """Whether ``log`` reaches its end, waiting out a scroll that lags a refresh."""
    for _ in range(100):
        if log.scroll_y == log.max_scroll_y:
            return True
        await pilot.pause()
    return False


async def test_escape_holds_the_cursor_and_the_conversation_stops_following() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        await pilot.press("k", "enter", "home", "j", "escape")
        log = visible_log(app)
        assert app.focused is None and log.highlighted == 1
        await feed_events(session, pilot, said("agent-1", "later"))
        assert log.scroll_y == 0 and log.highlighted == 1
        assert gutters(app)[1] == GUTTER_MARKER


async def test_enter_resumes_at_the_held_entry() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        await pilot.press("k", "enter", "home", "j", "escape")
        await feed_events(session, pilot, said("agent-1", "later"))
        await pilot.press("enter")
        log = visible_log(app)
        assert app.focused is log and log.highlighted == 1


async def test_a_second_escape_lets_go_and_follows_the_latest_output_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        await pilot.press("k", "enter", "home", "escape", "escape")
        log = visible_log(app)
        assert log.highlighted is None
        assert GUTTER_MARKER not in gutters(app)
        assert await scrolled_to_end(pilot, log)
        await feed_events(session, pilot, *(said("agent-1", LONG) for _ in range(3)))
        assert log.scroll_y == log.max_scroll_y
        await pilot.press("enter")
        assert log.highlighted == log.option_count - 1


async def test_escape_with_another_window_selected_keeps_the_held_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 3)
        await pilot.press("k", "enter", "k", "escape", "l", "escape", "j", "escape")
        assert visible_log(app).highlighted == 1


async def test_a_second_escape_with_no_entries_does_nothing_harmful() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("k", "enter", "escape", "escape")
        log = visible_log(app)
        assert app.focused is None and log.highlighted is None and log.option_count == 0


async def test_each_tab_holds_its_own_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "a"))
        await pilot.press("k", "enter", "k", "escape")  # All holds its first entry.
        await pilot.press("tab", "tab")  # coder
        coder = visible_log(app)
        assert coder.highlighted is None
        await pilot.press("enter", "escape", "escape")  # Held, then let go, in coder only.
        assert coder.highlighted is None
        await pilot.press("enter", "escape")  # Held again in coder.
        await pilot.press("shift+tab", "shift+tab")  # All, which kept its cursor.
        assert app.query_one(Transcript).only is None
        assert visible_log(app).highlighted == 0
        await pilot.press("escape")  # Lets go of All's only.
        assert visible_log(app).highlighted is None
        assert coder.highlighted == 1


async def scrolled_to_top(pilot: Pilot[None], log: TranscriptLog) -> bool:
    """Whether ``log`` reaches its top, waiting out a scroll that lags a refresh."""
    for _ in range(100):
        if log.scroll_y == 0:
            return True
        await pilot.pause()
    return False


async def held_out_of_view(
    session: FakeSession, pilot: Pilot[None], app: OrbitApp
) -> TranscriptLog:
    """The shown log, holding its first entry with the view scrolled away to the end."""
    await fed(session, pilot, app, 40)
    await pilot.press("k", "enter", "home", "escape")
    log = visible_log(app)
    log.scroll_end(animate=False)
    await pilot.pause()
    assert log.scroll_y > 0
    return log


async def test_switching_back_scrolls_to_a_held_entry_out_of_view() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        log = await held_out_of_view(session, pilot, app)
        await pilot.press("ctrl+t", "ctrl+t")
        assert await scrolled_to_top(pilot, log) and log.highlighted == 0


async def test_show_place_scrolls_back_to_a_held_entry() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        log = await held_out_of_view(session, pilot, app)
        log.show_place()
        assert await scrolled_to_top(pilot, log) and log.highlighted == 0


async def test_switching_tabs_keeps_a_held_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        await fed(session, pilot, app, 40)
        await pilot.press("k", "enter", "home", "ctrl+t")  # Away while in list mode.
        await pilot.press("ctrl+t")
        log = visible_log(app)
        assert app.focused is None and log.highlighted == 0
        for _ in range(5):
            await pilot.pause()
        assert log.scroll_y == 0 and log.max_scroll_y > 0


class LongLogApp(App[None]):
    """A shown log of 1,000 entries of two lines each, the ``tool`` ones collapsed."""

    def compose(self) -> ComposeResult:
        log = TranscriptLog(shown=True)
        for i in range(1000):
            log.append(Entry(f"entry {i}\nmore", "tool" if i % 2 else "agent"))
        yield log


def count_renders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The labels of the entries rendered or measured from now on, one per time."""
    renders: list[str] = []
    render = GutteredEntry.__rich_console__

    def counted(self: GutteredEntry, console: Console, options: ConsoleOptions) -> RenderResult:
        renders.append(self.label.plain)
        return render(self, console, options)

    monkeypatch.setattr(GutteredEntry, "__rich_console__", counted)
    return renders


def entry_number(label: str) -> int:
    return int(label.split()[2])


async def test_moving_the_cursor_in_a_long_log_draws_only_the_entries_it_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = LongLogApp()
    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one(TranscriptLog)
        log.enter_list_mode()
        await pilot.pause()
        renders = count_renders(monkeypatch)
        await pilot.press(*["k"] * 20)
        await pilot.pause()
        assert log.highlighted == 979
        assert gutters_of(log)[978:981] == [GUTTER_BAR, GUTTER_MARKER, GUTTER_BAR]
        assert 0 < len(renders) <= 20 * 10
        assert min(entry_number(label) for label in renders) >= 970


async def test_toggling_an_entry_measures_only_it_and_the_entries_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = LongLogApp()
    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one(TranscriptLog)
        log.enter_list_mode()
        await pilot.pause()
        height = log.virtual_size.height
        renders = count_renders(monkeypatch)
        log.toggle(995)
        await pilot.pause()
        assert log.virtual_size.height == height + 1
        assert min(entry_number(label) for label in renders) == 995
        assert len(renders) <= 50
        log.scroll_end(animate=False)
        assert await scrolled_to_end(pilot, log)
        assert [row[2:] for row in rendered_tail(log, 13)] == [
            "▾ entry 995",
            "more",
            "",
            "▾ entry 996",
            "more",
            "",
            "▸ entry 997 (+1 line)",
            "",
            "▾ entry 998",
            "more",
            "",
            "▸ entry 999 (+1 line)",
            "",
        ]


def gutters_of(log: TranscriptLog) -> list[str]:
    entries = []
    for index in range(log.option_count):
        prompt = log.get_option_at_index(index).prompt
        assert isinstance(prompt, GutteredEntry)
        entries.append(prompt.gutter.strip())
    return entries


def rendered_tail(log: TranscriptLog, rows: int) -> list[str]:
    """The text of the last ``rows`` rows on screen as drawn."""
    height = log.scrollable_content_region.height
    return [log.render_line(y).text.rstrip() for y in range(height - rows, height)]


class LogApp(App[None]):
    """A shown log, empty until a test appends to it."""

    def compose(self) -> ComposeResult:
        yield TranscriptLog(shown=True)


def entry_lines(log: TranscriptLog, index: int) -> list[list[Segment]]:
    """The rows of the entry at ``index`` as drawn at the log's width, gutter and all."""
    console = Console(width=log.size.width)
    return console.render_lines(entry(log, index), console.options, pad=False)


def entry_rows(log: TranscriptLog, index: int) -> list[str]:
    """The text of the rows of the entry at ``index``, gutter and all."""
    return ["".join(segment.text for segment in line) for line in entry_lines(log, index)]


async def test_an_agents_message_of_several_lines_hangs_its_marker_beside_its_markdown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", "**intro**\n\n- one\n- two"))
        assert [row.rstrip() for row in entry_rows(visible_log(app), 0)] == [
            "│ agent-1",
            "│ ▾ intro",
            "│",
            "│    • one",
            "│    • two",
            "",
        ]


async def test_an_agents_message_wraps_beside_its_hanging_marker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", f"{LONG}\n\nend"))
        rows = entry_rows(visible_log(app), 0)
        assert rows[0] == f"{GUTTER_BAR} agent-1"
        assert rows[1].startswith(f"{GUTTER_BAR} ▾ gutter")
        assert all(row.startswith(f"{GUTTER_BAR}   ") for row in rows[2:-1])
        words = " ".join(row[4:].strip() for row in rows[1:-1]).split()
        assert words == [*LONG.split(), "end"]


async def test_a_collapsed_agents_message_shows_its_first_line_as_it_is() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", "**intro**\n\n- one"))
        log = visible_log(app)
        log.toggle(0)
        await pilot.pause()
        assert prompts(app)[0].body is None
        assert [row.rstrip() for row in entry_rows(log, 0)] == [
            "│ agent-1",
            "│ ▸ **intro** (+2 lines)",
            "",
        ]


async def test_an_agents_message_of_one_line_has_no_marker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", "**hi** `there`"))
        assert [row.rstrip() for row in entry_rows(visible_log(app), 0)] == [
            "│ agent-1",
            "│ hi there",
            "",
        ]


@pytest.mark.parametrize(
    "text",
    [
        "```python\nunclosed = 1\n",
        "stray * and _ and ** marks_",
        "",
        "| a | b |\n|---|---|\n| " + "wide " * 40 + "| " + "cell " * 40 + "|",
        "```\n" + "x" * 300 + "\n```",
        "one\n\n```\n" + "y" * 300 + "\n```",
    ],
)
async def test_awkward_markdown_stays_within_the_log(text: str) -> None:
    app = LogApp()
    async with app.run_test(size=(40, 24)) as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry(text, "assistant"))
        await pilot.pause()
        rows = entry_rows(log, 0)
        assert len(rows) >= 2
        assert rows[-1] == ""
        assert all(Text(row).cell_len <= log.size.width for row in rows)
        assert all(row.startswith(f"{GUTTER_BAR} ") for row in rows[:-1])


@pytest.mark.parametrize("text", ["\n\n", " ", "<!-- x -->", "[a]: https://x"])
async def test_markdown_that_renders_nothing_still_draws_a_gutter_row(text: str) -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry(text, "assistant"))
        await pilot.pause()
        assert entry_rows(log, 0)[0].startswith(GUTTER_BAR)
        assert entry_rows(log, 0)[-1] == ""
        log.focus()
        log.highlighted = 0
        await pilot.pause()
        assert entry_rows(log, 0)[0].startswith(GUTTER_MARKER)


async def test_a_submitted_prompt_is_added_as_a_prompt_entry() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        assert app.query_one(Transcript).entries(None) == (
            Entry("hi", "prompt", PLANNER, key=PLANNER),
        )
        assert [row.rstrip() for row in entry_rows(visible_log(app), 0)] == ["│ ❯ hi", ""]


async def appended(*entries: tuple[str, EntryKind]) -> list[list[str]]:
    """The rows of each of ``entries`` appended to a log, as drawn."""
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        for text, kind in entries:
            log.append(Entry(text, kind))
        await pilot.pause()
        return [[row.rstrip() for row in entry_rows(log, i)] for i in range(len(entries))]


async def test_a_prompt_hangs_its_markers_beside_its_markdown() -> None:
    (rows,) = await appended(("**intro**\n\n- one\n- two", "prompt"))
    assert rows == ["│ ❯ ▾ intro", "│", "│      • one", "│      • two", ""]


async def test_a_prompt_renders_a_fenced_code_block() -> None:
    (rows,) = await appended(("run:\n\n```\nx = 1\n```", "prompt"))
    assert rows[0] == "│ ❯ ▾ run:"
    assert "│      x = 1" in rows
    assert not any("```" in row for row in rows)


def backgrounds(line: list[Segment]) -> list[Color | None]:
    """The background of each segment of ``line``."""
    return [None if segment.style is None else segment.style.bgcolor for segment in line]


async def test_every_row_of_a_prompt_is_banded_across_the_width_beside_the_gutter() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry(f"{LONG}\n\nend", "prompt"))
        await pilot.pause()
        band = log.get_component_rich_style("transcript--prompt")
        assert band.bgcolor is not None and not band.bold
        lines = entry_lines(log, 0)
        assert len(lines) > 3 and backgrounds(lines[-1]) == []
        for line in lines[:-1]:
            gutter, *rest = backgrounds(line)
            assert gutter is None and set(rest) == {band.bgcolor}
            assert Segment.get_line_length(line) == log.size.width


async def test_task_headers_stay_bold_and_plain_without_a_band() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry("Task 3: **do** it", "user"))
        await pilot.pause()
        header = entry(log, 0)
        assert header.band is None and header.body is None
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ Task 3: **do** it", ""]
        assert label_style(header).bold


def prompt_of(lines: int) -> str:
    return "\n".join(f"line {i}" for i in range(lines))


async def test_a_prompt_longer_than_its_lines_shown_starts_collapsed() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry(prompt_of(PROMPT_LINES_SHOWN + 1), "prompt"))
        log.append(Entry(prompt_of(PROMPT_LINES_SHOWN), "prompt"))
        await pilot.pause()
        assert log.collapsed(0) and not log.collapsed(1)
        assert entry(log, 0).body is None
        assert entry(log, 0).label.plain == f"❯ ▸ line 0 (+{PROMPT_LINES_SHOWN} lines)"
        assert entry(log, 1).body is not None


async def test_a_prompt_toggles_between_collapsed_and_expanded() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry("one\n\ntwo", "prompt"))
        await pilot.pause()
        log.toggle(0)
        await pilot.pause()
        assert log.collapsed(0)
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ ❯ ▸ one (+2 lines)", ""]
        log.toggle(0)
        await pilot.pause()
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ ❯ ▾ one", "│", "│     two", ""]


@pytest.mark.parametrize("kind", ["user", "tool", "verify", "agent", "request", "error"])
async def test_entries_other_than_agents_messages_show_their_text_as_it_is(
    kind: EntryKind,
) -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry("# **a**", kind))
        log.append(Entry("# **a**\n- b", kind))
        await pilot.pause()
        if log.collapsed(1):
            log.toggle(1)
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ # **a**", ""]
        assert [row.rstrip() for row in entry_rows(log, 1)] == ["│ ▾ # **a**", "│ - b", ""]


def headings(app: OrbitApp) -> list[str | None]:
    """The label row heading each of the shown log's entries, if it has one."""
    return [None if prompt.heading is None else prompt.heading.plain for prompt in prompts(app)]


async def test_an_agents_first_message_is_headed_by_a_muted_label_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", "hi"))
        log = visible_log(app)
        assert headings(app) == ["agent-1"]
        heading = prompts(app)[0].heading
        assert heading is not None
        style = heading.get_style_at_offset(Console(), 0)
        assert style == log.get_component_rich_style("transcript--label", partial=True)
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ agent-1", "│ hi", ""]


async def test_the_label_row_takes_the_cursors_marker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", "hi"))
        await pilot.press("k", "enter")
        rows = entry_rows(visible_log(app), 0)
        assert [row.rstrip() for row in rows] == ["▌ agent-1", "▌ hi", ""]


async def test_an_agents_messages_in_a_row_are_labelled_once() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            said("agent-1", "looking"),
            tool_output("agent-1", "found"),
            said("agent-1", "done"),
        )
        assert headings(app) == ["agent-1", None, None]


async def test_another_agent_in_between_brings_the_label_back() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            said("agent-1", "first"),
            said("agent-2", "second"),
            said("agent-1", "third"),
        )
        assert headings(app) == ["agent-1", "agent-2", "agent-1"]


async def test_the_planners_messages_are_never_labelled() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        await feed_events(
            session,
            pilot,
            AssistantMessage(turn_id="t", text="planning"),
            said("agent-1", "coding"),
            AssistantMessage(turn_id="t", text="planned"),
        )
        assert headings(app) == [None, None, "agent-1", None]


async def test_a_real_planners_messages_are_never_labelled() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1", "planner"), started("agent-2"))
        await pilot.press("i", *"hi", "enter")
        await feed_events(
            session,
            pilot,
            said("agent-1", "planning"),
            said("agent-2", "coding"),
            said("agent-1", "planned"),
        )
        assert headings(app) == [None, None, None, None, "agent-2", None]
        await pilot.press("ctrl+t")  # The planner tab.
        assert labels(app) == ["▶ agent-1 (planner) started", "❯ hi", "planning", "planned"]
        assert headings(app) == [None, None, None, None]


async def test_the_speaker_is_judged_within_each_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session,
            pilot,
            started("agent-1"),
            started("agent-2", "reviewer"),
            said("agent-1", "coding"),
            said("agent-2", "reviewing"),
            said("agent-1", "more"),
        )
        # Under All the reviewer speaks in between; under coder agent-1 speaks throughout.
        assert headings(app) == [None, None, "agent-1", "agent-2", "agent-1"]
        await pilot.press("tab", "tab")  # coder
        assert labels(app) == ["▶ agent-1 (coder) started", "coding", "more"]
        assert headings(app) == [None, None, None]


async def test_a_collapsed_agents_message_keeps_its_label_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, said("agent-1", "one\ntwo"))
        log = visible_log(app)
        log.toggle(0)
        await pilot.pause()
        assert log.collapsed(0)
        assert headings(app) == ["agent-1"]
        assert entry_rows(log, 0)[0].rstrip() == "│ agent-1"


def thought(agent_id: str, text: str, turn: str = "t") -> AssistantMessage:
    return AssistantMessage(turn_id=turn, text=text, agent_id=agent_id)


def tool_call(agent_id: str, turn: str = "t") -> ToolCallStarted:
    return ToolCallStarted(turn_id=turn, call_id="c", name="Read", arguments={}, agent_id=agent_id)


def turn_ended(agent_id: str, turn: str = "t", reason: TurnEndReason = "completed") -> TurnFinished:
    return TurnFinished(turn_id=turn, agent_id=agent_id, reason=reason)


def brightness(app: OrbitApp) -> list[str]:
    """How each of the shown log's agents' messages is drawn: dim or bright."""
    log = visible_log(app)
    dim = log.get_component_rich_style("transcript--thinking", partial=True)
    bright = log.get_component_rich_style("transcript--assistant", partial=True)
    names = {dim: "dim", bright: "bright"}
    return [names[label_style(prompt)] for prompt in prompts(app) if is_markdown(prompt)]


def is_markdown(prompt: GutteredEntry) -> bool:
    """Whether ``prompt`` draws markdown: an agent's message rather than a rule, say."""
    return isinstance(prompt.body, HangingIndent)


async def test_an_agents_message_arrives_dim_under_a_muted_label_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, thought("agent-1", "looking"))
        log = visible_log(app)
        assert brightness(app) == ["dim"]
        thinking = log.get_component_rich_style("transcript--thinking", partial=True)
        assert thinking != log.get_component_rich_style("transcript--assistant", partial=True)
        heading = prompts(app)[0].heading
        assert heading is not None
        label = log.get_component_rich_style("transcript--label", partial=True)
        assert heading.get_style_at_offset(Console(), 0) == label


async def test_only_the_message_after_the_last_tool_call_brightens_when_the_turn_ends() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "looking"),
            tool_call("agent-1"),
            tool_output("agent-1", "found"),
            thought("agent-1", "done"),
        )
        assert brightness(app) == ["dim", "dim"]
        await feed_events(session, pilot, turn_ended("agent-1"))
        assert brightness(app) == ["dim", "bright"]


@pytest.mark.parametrize("reason", ["completed", "cancelled", "error", "max_steps"])
async def test_a_turn_of_a_message_alone_brightens_it_however_it_ends(
    reason: TurnEndReason,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session, pilot, thought("agent-1", "done"), turn_ended("agent-1", reason=reason)
        )
        assert brightness(app) == ["bright"]


async def test_a_turn_ending_on_a_tool_call_brightens_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "looking"),
            tool_call("agent-1"),
            turn_ended("agent-1"),
        )
        assert brightness(app) == ["dim"]


async def test_every_message_after_the_last_tool_call_brightens() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            tool_call("agent-1"),
            thought("agent-1", "found it"),
            thought("agent-1", "done"),
            turn_ended("agent-1"),
        )
        assert brightness(app) == ["bright", "bright"]


@pytest.mark.parametrize("first", ["agent-1", "agent-2"])
async def test_concurrent_turns_brighten_only_their_own_messages(first: str) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    turns = {"agent-1": "t1", "agent-2": "t2"}
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "one", "t1"),
            thought("agent-2", "two", "t2"),
            turn_ended(first, turns[first]),
        )
        expected = {"agent-1": ["bright", "dim"], "agent-2": ["dim", "bright"]}[first]
        assert brightness(app) == expected
        last = "agent-2" if first == "agent-1" else "agent-1"
        await feed_events(session, pilot, turn_ended(last, turns[last]))
        assert brightness(app) == ["bright", "bright"]


@pytest.mark.parametrize("reason", ["killed", "crashed"])
async def test_an_agent_exiting_mid_turn_brightens_its_final_answer(
    reason: AgentExitReason,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "done"),
            AgentExited(agent_id="agent-1", reason=reason),
        )
        assert brightness(app) == ["bright"]


async def test_an_agent_exiting_mid_turn_on_a_tool_call_brightens_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "looking"),
            tool_call("agent-1"),
            AgentExited(agent_id="agent-1", reason="crashed"),
        )
        assert brightness(app) == ["dim"]


async def test_an_agent_exiting_after_its_turn_ended_keeps_its_final_answer_bright() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "done"),
            turn_ended("agent-1"),
            AgentExited(agent_id="agent-1", reason="finished"),
        )
        assert brightness(app) == ["bright"]


async def test_an_agent_exiting_leaves_another_agents_open_turn_dim() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "one", "t1"),
            thought("agent-2", "two", "t2"),
            AgentExited(agent_id="agent-1", reason="killed"),
        )
        assert brightness(app) == ["bright", "dim"]


async def test_a_final_answer_brightens_under_all_and_its_roles_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session,
            pilot,
            started("agent-1"),
            started("agent-2", "reviewer"),
            thought("agent-1", "done", "t1"),
            thought("agent-2", "reviewing", "t2"),
            turn_ended("agent-1", "t1"),
        )
        assert brightness(app) == ["bright", "dim"]
        await pilot.press("tab", "tab")  # coder
        assert brightness(app) == ["bright"]
        transcript = app.query_one(Transcript)
        assert transcript.entries("coder")[1] == Entry("done", "assistant", "agent-1", key="coder")


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


async def test_t_hides_dim_messages_and_shows_them_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, *thinking_turn())
        shown = labels(app)
        await pilot.press("t")
        assert labels(app) == TOOL_ROWS
        await pilot.press("t")
        assert labels(app) == shown


async def test_hidden_thinking_leaves_bright_messages_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, *thinking_turn(), turn_ended("agent-1"))
        await pilot.press("t")
        assert labels(app) == [*TOOL_ROWS, "done", "agent-1 done"]
        assert brightness(app) == ["bright"]


async def test_hiding_thinking_applies_to_a_roles_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), *thinking_turn())
        await pilot.press("tab", "tab")  # coder
        await pilot.press("t")
        assert labels(app) == ["▶ agent-1 (coder) started", *TOOL_ROWS]


async def test_a_tab_opened_while_thinking_is_hidden_hides_it_too() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await feed_events(session, pilot, started("agent-1"), *thinking_turn())
        await pilot.press("tab", "tab")  # coder, whose log was made after the press.
        assert labels(app) == ["▶ agent-1 (coder) started", *TOOL_ROWS]


async def test_the_title_notes_hidden_thinking_on_all_and_on_a_tab() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(120, 30)) as pilot:
        transcript = app.query_one(Transcript)
        await pilot.press("t")
        assert transcript.border_title == "Conversation · thinking hidden"
        await pilot.press("tab", "tab")  # coder
        assert transcript.border_title == "Conversation · coder · thinking hidden"
        await pilot.press("t")
        assert transcript.border_title == "Conversation · coder"


async def test_a_hidden_cursors_entry_moves_the_cursor_to_the_entry_before_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, tool_output("agent-1", "found"), *thinking_turn())
        await pilot.press("k", "enter")  # At the latest entry, a dim message.
        log = visible_log(app)
        await pilot.press("t")
        assert app.focused is log
        assert log.highlighted == 1 and labels(app)[1] == "agent-1: → Read ✓ 1 line"
        assert gutters(app) == [GUTTER_BAR, GUTTER_MARKER]


async def test_a_hidden_cursors_entry_with_none_before_it_moves_to_the_one_after() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, *thinking_turn())
        await pilot.press("k", "enter", "home")  # At the first entry, a dim message.
        await pilot.press("t")
        assert visible_log(app).highlighted == 0 and gutters(app) == [GUTTER_MARKER]


async def test_a_hidden_cursors_entry_with_no_entry_left_leaves_no_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, thought("agent-1", "looking"))
        await pilot.press("k", "enter", "t")
        log = visible_log(app)
        assert log.option_count == 0 and log.highlighted is None


async def test_the_cursor_stays_on_its_entry_when_thinking_is_shown_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, *thinking_turn())
        await pilot.press("t", "k", "enter")  # On the tool call, the latest entry shown.
        await pilot.press("t")
        log = visible_log(app)
        assert log.highlighted == 1
        assert gutters(app) == [GUTTER_BAR, GUTTER_MARKER, GUTTER_BAR]


async def test_collapsed_entries_stay_so_across_hiding_and_showing_thinking() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, thought("agent-1", "one\ntwo"), turn_ended("agent-1"))
        log = visible_log(app)
        log.toggle(0)
        await pilot.press("t", "t")
        assert log.collapsed(0)


async def test_a_message_promoted_while_thinking_is_hidden_appears() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, *thinking_turn())
        await pilot.press("t")
        await feed_events(session, pilot, turn_ended("agent-1"))
        assert labels(app) == [*TOOL_ROWS, "done", "agent-1 done"]
        assert brightness(app) == ["bright"]


async def test_a_turn_ending_on_a_tool_call_while_hidden_shows_only_its_rule() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, thought("agent-1", "looking"), tool_call("agent-1"))
        await pilot.press("t")
        await feed_events(session, pilot, turn_ended("agent-1"))
        assert labels(app) == ["agent-1: → Read ⊘ no result", "agent-1 done"]


async def test_speaker_labels_are_judged_on_the_entries_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            thought("agent-1", "first", "t1"),
            turn_ended("agent-1", "t1"),
            thought("agent-2", "thinking", "t2"),
            thought("agent-1", "second", "t3"),
            turn_ended("agent-1", "t3"),
        )
        assert headings(app) == ["agent-1", None, "agent-2", "agent-1", None]
        await pilot.press("t")
        assert headings(app) == ["agent-1", None, None, None]
        await pilot.press("t")
        assert headings(app) == ["agent-1", None, "agent-2", "agent-1", None]


async def test_t_types_into_the_prompt_rather_than_hiding_thinking() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, thought("agent-1", "looking"))
        await pilot.press("i", "t")
        assert app.query_one(ChatPrompt).text == "t"
        assert not app.query_one(Transcript).thinking_hidden
        assert labels(app) == ["looking"]


async def test_t_does_nothing_on_a_modal_screen() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e", "t")
        assert not app.query_one(Transcript).thinking_hidden


def turn_began(agent_id: str, turn: str = "t") -> TurnStarted:
    return TurnStarted(turn_id=turn, agent_id=agent_id)


async def test_a_turns_rule_follows_its_last_entry_timed_from_its_start() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    now = [100.0]
    app.clock = lambda: now[0]
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-3"), turn_began("agent-3"))
        now[0] = 350.0
        await feed_events(session, pilot, thought("agent-3", "done"), turn_ended("agent-3"))
        transcript = app.query_one(Transcript)
        rule = Entry("agent-3 done · 4m10s", "rule", "agent-3", key="coder")
        for key in (None, "coder"):
            assert transcript.entries(key)[-2:] == (
                Entry("done", "assistant", "agent-3", key="coder"),
                rule,
            )


async def test_a_turns_rule_with_usage_shows_its_time_and_tokens() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        usage = Usage(input_tokens=210_000, output_tokens=12_000, duration_ms=72_000)
        ended = TurnFinished(turn_id="t", agent_id="agent-3", reason="completed", usage=usage)
        await feed_events(session, pilot, turn_began("agent-3"), ended)
        assert labels(app) == ["agent-3 done · 1m12s · ↑210k ↓12k"]


async def test_a_turn_whose_error_was_shown_still_ends_in_an_error_rule() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            ErrorOccurred(agent_id="agent-3", message="boom"),
            turn_ended("agent-3", reason="error"),
        )
        entries = app.query_one(Transcript).entries(None)
        assert [(entry.text, entry.kind) for entry in entries] == [
            ("agent-3: boom", "error"),
            ("agent-3 error", "error_rule"),
        ]


@pytest.mark.parametrize(
    ("reason", "component"),
    [("completed", "rule"), ("max_steps", "rule"), ("error", "error_rule")],
)
async def test_a_turns_rule_is_drawn_across_the_width_in_its_style(
    reason: TurnEndReason, component: str
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(60, 20)) as pilot:
        await feed_events(session, pilot, turn_ended("agent-3", reason=reason))
        log = visible_log(app)
        style = log.get_component_rich_style(f"transcript--{component}", partial=True)
        assert label_style(entry(log, 0)) == style
        row = entry_rows(log, 0)[0]
        assert row.startswith(f"{GUTTER_BAR} ──")
        assert row.endswith("──") and len(row) == log.size.width
        segments = [s for s in entry_lines(log, 0)[0] if s.text.strip("─ ")]
        assert all(segment.style == style for segment in segments[1:])


async def test_rules_and_errors_have_their_own_styles() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        log = visible_log(app)
        rule = log.get_component_rich_style("transcript--rule", partial=True)
        error_rule = log.get_component_rich_style("transcript--error_rule", partial=True)
        assert rule == log.get_component_rich_style("transcript--tool", partial=True)
        assert error_rule == log.get_component_rich_style("transcript--error", partial=True)


def texts(app: OrbitApp, key: str | None) -> list[tuple[str, EntryKind]]:
    return [(entry.text, entry.kind) for entry in app.query_one(Transcript).entries(key)]


async def test_an_agent_finishing_after_its_turn_ends_in_that_one_rule() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    app.clock = lambda: 0.0
    async with app.run_test() as pilot:
        exit_ = AgentExited(agent_id="agent-3", reason="finished")
        await feed_events(session, pilot, started("agent-3"), turn_began("agent-3"))
        await feed_events(session, pilot, turn_ended("agent-3"), exit_)
        assert texts(app, None) == [
            ("▶ agent-3 (coder) started", "agent"),
            ("agent-3 done · 0s", "rule"),
        ]


async def test_an_agent_crashing_mid_turn_ends_in_a_crashed_rule_timed_from_the_turn() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    now = [100.0]
    app.clock = lambda: now[0]
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-3"), turn_ended("agent-3"))
        now[0] = 120.0
        await feed_events(session, pilot, turn_began("agent-3", "t2"))
        now[0] = 162.0
        await feed_events(session, pilot, AgentExited(agent_id="agent-3", reason="crashed"))
        assert texts(app, "coder")[-2:] == [
            ("agent-3 done", "rule"),
            ("agent-3 crashed · 42s", "error_rule"),
        ]


async def test_an_agent_exiting_before_any_turn_is_timed_from_its_start() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    now = [10.0]
    app.clock = lambda: now[0]
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-3"))
        now[0] = 82.0
        await feed_events(session, pilot, AgentExited(agent_id="agent-3", reason="killed"))
        assert texts(app, None)[-1] == ("agent-3 stopped · 1m12s", "rule")


async def test_an_agent_exiting_unseen_ends_in_an_untimed_rule() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, AgentExited(agent_id="agent-3", reason="finished"))
        assert texts(app, None) == [("agent-3 done", "rule")]


async def test_an_agent_killed_after_a_done_rule_turns_that_rule_stopped() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    app.clock = lambda: 0.0
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-3"), turn_began("agent-3"))
        await feed_events(session, pilot, turn_ended("agent-3"), said("agent-1", "elsewhere"))
        await feed_events(session, pilot, AgentExited(agent_id="agent-3", reason="killed"))
        for key in (None, "coder"):
            assert ("agent-3 stopped · 0s", "rule") in texts(app, key)
            assert ("agent-3 done · 0s", "rule") not in texts(app, key)
        assert labels(app)[-2:] == ["agent-3 stopped · 0s", "elsewhere"]


async def test_a_temporary_tab_agents_exit_rule_lands_in_its_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-3", "checker"))
        await feed_events(session, pilot, AgentExited(agent_id="agent-3", reason="crashed"))
        assert texts(app, "agent-3")[-1] == ("agent-3 crashed · 0s", "error_rule")


async def test_replacing_a_rule_in_a_log_without_one_leaves_it() -> None:
    app = LogApp()
    async with app.run_test():
        log = app.query_one(TranscriptLog)
        log.append(Entry("hi", "agent", "agent-3"))
        log.replace_rule("agent-3", "agent-3 stopped", "rule")
        assert log.entries == (Entry("hi", "agent", "agent-3"),)


def call(agent_id: str, call_id: str, path: str = "a.py") -> ToolCallStarted:
    return ToolCallStarted(
        turn_id="t", call_id=call_id, name="Read", arguments={"file_path": path}, agent_id=agent_id
    )


def result(agent_id: str, call_id: str, output: str, is_error: bool = False) -> ToolCallFinished:
    return ToolCallFinished(
        turn_id="t", call_id=call_id, output=output, is_error=is_error, agent_id=agent_id
    )


def still(app: OrbitApp, at: float = 0.0) -> Transcript:
    """The app's transcript, its spinners stopped at ``at`` seconds."""
    transcript = app.query_one(Transcript)
    transcript.clock = lambda: at
    return transcript


async def test_a_tool_call_and_its_result_are_one_entry_updated_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        still(app)
        await feed_events(session, pilot, call("agent-1", "c1"))
        assert labels(app) == ["agent-1: ◜ Read a.py"]
        await feed_events(session, pilot, result("agent-1", "c1", "one\ntwo"))
        assert labels(app) == ["agent-1: → Read a.py ✓ 2 lines"]
        log = visible_log(app)
        assert label_style(prompts(app)[0]) == log.get_component_rich_style(
            "transcript--tool", partial=True
        )


async def test_a_tool_calls_entry_keeps_its_full_arguments_and_output() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, call("agent-1", "c1"), result("agent-1", "c1", "x\ny"))
        kept = app.query_one(Transcript).entries(None)[0].call
        assert kept is not None
        assert (kept.arguments, kept.output) == ({"file_path": "a.py"}, "x\ny")


async def test_a_failed_tool_call_is_crossed_in_the_error_style() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session, pilot, call("agent-1", "c1"), result("agent-1", "c1", "boom", is_error=True)
        )
        assert labels(app) == ["agent-1: → Read a.py ✗ 1 line"]
        log = visible_log(app)
        assert label_style(prompts(app)[0]) == log.get_component_rich_style(
            "transcript--error", partial=True
        )


async def test_concurrent_tool_calls_finishing_out_of_order_update_their_own_entries() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        still(app)
        await feed_events(
            session,
            pilot,
            call("agent-1", "c1", "a.py"),
            call("agent-1", "c2", "b.py"),
            result("agent-1", "c2", ""),
        )
        assert labels(app) == ["agent-1: ◜ Read a.py", "agent-1: → Read b.py ✓ 0 lines"]
        await feed_events(session, pilot, result("agent-1", "c1", "x"))
        assert labels(app) == ["agent-1: → Read a.py ✓ 1 line", "agent-1: → Read b.py ✓ 0 lines"]


async def test_a_result_updates_only_the_call_of_its_own_agent() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        still(app)
        await feed_events(
            session, pilot, call("agent-1", "c"), call("agent-2", "c"), result("agent-2", "c", "")
        )
        assert labels(app) == ["agent-1: ◜ Read a.py", "agent-2: → Read a.py ✓ 0 lines"]


async def test_a_tool_call_unfinished_when_its_turn_ends_has_no_result() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        transcript = still(app)
        await feed_events(
            session, pilot, call("agent-1", "c1"), call("agent-2", "c2"), turn_ended("agent-1")
        )
        assert labels(app)[0] == "agent-1: → Read a.py ⊘ no result"
        assert labels(app)[1] == "agent-2: ◜ Read a.py"
        assert transcript.ticking


async def test_a_tool_call_unfinished_when_its_agent_exits_has_no_result() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            call("agent-1", "c1"),
            AgentExited(agent_id="agent-1", reason="crashed"),
        )
        assert labels(app)[0] == "agent-1: → Read a.py ⊘ no result"
        assert not app.query_one(Transcript).ticking


async def test_a_result_after_its_turn_ended_is_an_entry_of_its_own() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session,
            pilot,
            call("agent-1", "c1"),
            turn_ended("agent-1"),
            result("agent-1", "c1", "x"),
        )
        assert labels(app) == ["agent-1: → Read a.py ⊘ no result", "agent-1 done", "agent-1: x"]


async def test_a_tool_call_updates_under_all_and_its_roles_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session, pilot, started("agent-1"), call("agent-1", "c1"), result("agent-1", "c1", "x")
        )
        assert labels(app)[1] == "agent-1: → Read a.py ✓ 1 line"
        await pilot.press("tab", "tab")  # coder
        assert labels(app) == ["▶ agent-1 (coder) started", "agent-1: → Read a.py ✓ 1 line"]


async def test_a_tool_call_finishing_after_hidden_thinking_updates_its_own_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("t")
        await feed_events(
            session,
            pilot,
            thought("agent-1", "looking"),
            tool_output("agent-1", "earlier"),
            call("agent-1", "c1"),
            result("agent-1", "c1", "x"),
        )
        assert labels(app) == ["agent-1: earlier", "agent-1: → Read a.py ✓ 1 line"]


async def test_a_running_tool_calls_spinner_turns_on_each_tick() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        transcript = still(app)
        await feed_events(session, pilot, Notice(message="n"), call("agent-1", "c1"))
        assert transcript.ticking
        transcript.clock = lambda: 1 / SPINNER_FPS
        transcript.tick()
        assert labels(app) == ["📌 n", "agent-1: ◠ Read a.py"]


async def test_spinners_stop_once_no_tool_call_runs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        transcript = still(app)
        await feed_events(session, pilot, call("agent-1", "c1"), result("agent-1", "c1", "x"))
        assert not transcript.ticking
        transcript.clock = lambda: 1 / SPINNER_FPS
        transcript.tick()
        assert labels(app) == ["agent-1: → Read a.py ✓ 1 line"]


async def test_a_tick_within_a_frame_leaves_the_spinner_as_it_is() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        transcript = still(app)
        await feed_events(session, pilot, call("agent-1", "c1"))
        before = prompts(app)[0]
        transcript.tick()
        assert prompts(app)[0] is before


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


async def test_a_request_to_orbit_and_its_reply_are_one_entry_updated_in_place() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        transcript = still(app)
        await feed_events(session, pilot, ask("agent-1"))
        assert labels(app) == [f"◜ {ASKED}"]
        assert transcript.ticking
        await feed_events(session, pilot, answer("agent-1", "Checks passed.\nMerged."))
        assert labels(app) == [f"{ASKED} ✓ Checks passed."]
        assert not transcript.ticking
        assert label_style(prompts(app)[0]) == visible_log(app).get_component_rich_style(
            "transcript--request", partial=True
        )


async def test_a_refused_request_is_crossed_in_the_error_style() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ask("agent-1"), answer("agent-1", "No.", is_error=True))
        assert labels(app) == [f"{ASKED} ✗ No."]
        assert label_style(prompts(app)[0]) == visible_log(app).get_component_rich_style(
            "transcript--error", partial=True
        )


async def test_a_long_reply_is_clipped_to_one_line() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ask("agent-1"), answer("agent-1", "a" * 200))
        assert labels(app) == [f"{ASKED} ✓ {'a' * (REPLY_LIMIT - 1)}…"]


async def test_a_requests_entry_keeps_its_full_arguments_and_reply() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ask("agent-1"), answer("agent-1", "x\ny"))
        kept = app.query_one(Transcript).entries(None)[0].call
        assert kept is not None
        assert (kept.arguments, kept.output) == ({"summary": "Add a flag"}, "x\ny")


async def test_a_reply_to_an_unknown_request_is_an_entry_of_its_own() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        still(app)
        await feed_events(session, pilot, ask("agent-1"), answer("agent-1", "ok", "other"))
        assert labels(app) == [f"◜ {ASKED}", "orbit → agent-1: ok"]


async def test_a_reply_doesnt_finish_a_tool_call_of_the_same_id() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        still(app)
        await feed_events(session, pilot, call("agent-1", "r"), answer("agent-1", "ok"))
        assert labels(app) == ["agent-1: ◜ Read a.py", "orbit → agent-1: ok"]


async def test_a_request_keeps_waiting_when_its_turn_ends() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        still(app)
        await feed_events(session, pilot, ask("agent-1"), turn_ended("agent-1"))
        assert labels(app) == [f"◜ {ASKED}", "agent-1 done"]


async def test_a_request_unanswered_when_its_agent_exits_has_no_reply() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        transcript = still(app)
        await feed_events(
            session,
            pilot,
            ask("agent-1"),
            ask("agent-2"),
            AgentExited(agent_id="agent-1", reason="crashed"),
        )
        assert labels(app)[:2] == [
            f"{ASKED} ⊘ no reply",
            "◜ agent-2 → orbit complete_task Add a flag",
        ]
        assert label_style(prompts(app)[0]) == visible_log(app).get_component_rich_style(
            "transcript--tool", partial=True
        )
        assert transcript.ticking


async def test_a_request_updates_under_all_and_its_roles_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session, pilot, started("agent-1"), ask("agent-1"), answer("agent-1", "ok")
        )
        assert labels(app)[1] == f"{ASKED} ✓ ok"
        await pilot.press("tab", "tab")  # coder
        assert labels(app) == ["▶ agent-1 (coder) started", f"{ASKED} ✓ ok"]


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
