"""The transcript keeps entries per tab, each tab a list of entries that can be moved between
and collapsed."""

import pytest
from rich.console import Console, ConsoleOptions, RenderResult
from rich.style import Style
from textual.app import App, ComposeResult
from textual.pilot import Pilot

from orbit.protocol import AssistantMessage, Notice, ToolCallFinished
from orbit.tui import OrbitApp
from orbit.tui.transcript import GutteredEntry, Transcript, TranscriptLog
from orbit.tui.view import GUTTER_BAR, GUTTER_MARKER
from tests.test_tui import FakeSession, feed_events, said, started

# Wraps in any window a test opens. At 40 columns a row beside the gutter holds two words but
# would hold three if wrapped at the full width, so cropping a row would lose text.
LONG = " ".join(["gutter"] * 20)


def visible_log(app: OrbitApp) -> TranscriptLog:
    return app.query_one("Transcript > TranscriptLog.-shown", TranscriptLog)


def prompts(app: OrbitApp) -> list[GutteredEntry]:
    log = visible_log(app)
    entries = []
    for index in range(log.option_count):
        prompt = log.get_option_at_index(index).prompt
        assert isinstance(prompt, GutteredEntry)
        entries.append(prompt)
    return entries


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
        assert rendered(visible_log(app), 5) == [
            "│ ▶ agent-1 (coder) started",
            "",
            "│ ▾ agent-1: hi",
            "│ there",
            "",
        ]


async def test_entry_kinds_style_the_entries() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        await feed_events(session, pilot, AssistantMessage(turn_id="t", text="plain"))
        log = visible_log(app)
        user, plain = prompts(app)
        user_style, plain_style = (label_style(prompt) for prompt in (user, plain))
        assert user_style == log.get_component_rich_style("transcript--user", partial=True)
        assert isinstance(user_style, Style) and user_style.bold
        assert plain_style == log.get_component_rich_style("transcript--assistant", partial=True)
        assert user_style.bgcolor is None


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
        await feed_events(session, pilot, said("agent-1", LONG))
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
            log.append(f"entry {i}\nmore", "tool" if i % 2 else "assistant")
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
