"""The conversation window shows its entries, each tab's in a log of its own, routes the
app's keys and events to them, and scrolls each to its place."""

from rich.console import Console
from rich.style import Style
from textual.app import App, ComposeResult
from textual.pilot import Pilot

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AssistantMessage,
    Event,
    Notice,
    ToolCallFinished,
    ToolCallStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.entries import HangingIndent, entry_markdown
from orbit.tui.transcript import Transcript
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.common import SPINNER_FPS, EntryKind
from orbit.tui.view.log import Entry
from orbit.tui.view.transcript import GUTTER_BAR, GUTTER_MARKER, PROMPT_MARKER
from orbit.tui.widgets import ChatPrompt
from tests.tui_fakes import (
    LONG,
    STRIP,
    FakeSession,
    call,
    entry,
    entry_rows,
    feed_events,
    labels,
    prompts,
    said,
    scrolled_to_end,
    started,
    tab_strip,
    visible_log,
)


def gutters(app: OrbitApp) -> list[str]:
    """The gutter symbol of each of the shown log's entries."""
    return [prompt.gutter.strip() for prompt in prompts(app)]


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
            Entry("▶ agent-1 (coder) started", "agent", "agent-1", speaker="agent-1"),
            Entry("hi", "assistant", "agent-1", "t", speaker="agent-1"),
            Entry("📌 n", "verify"),
        )
        # agent-1 runs in a pinned role, so its entries belong to the role's tab.
        assert transcript.entries("coder") == transcript.entries(None)[:2]
        assert transcript.entries("agent-1") == ()


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


def test_a_hanging_indent_over_nothing_draws_its_first_prefix() -> None:
    console = Console(width=40)
    indent = HangingIndent(entry_markdown("<!-- x -->", Style()), PROMPT_MARKER, "  ", Style())
    lines = console.render_lines(indent, console.options, pad=False)
    assert ["".join(segment.text for segment in line) for line in lines] == [PROMPT_MARKER]


async def test_a_submitted_prompt_is_added_as_a_prompt_entry() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        assert app.query_one(Transcript).entries(None) == (Entry("hi", "prompt", PLANNER),)
        assert [row.rstrip() for row in entry_rows(visible_log(app), 0)] == ["│ ❯ hi", ""]


def thought(agent_id: str, text: str, turn: str = "t") -> AssistantMessage:
    return AssistantMessage(turn_id=turn, text=text, agent_id=agent_id)


def tool_call(agent_id: str, turn: str = "t") -> ToolCallStarted:
    return ToolCallStarted(turn_id=turn, call_id="c", name="Read", arguments={}, agent_id=agent_id)


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


def texts(app: OrbitApp, key: str | None) -> list[tuple[str, EntryKind]]:
    return [(entry.text, entry.kind) for entry in app.query_one(Transcript).entries(key)]


async def test_a_temporary_tab_agents_exit_rule_lands_in_its_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    now = [10.0]
    app.clock = lambda: now[0]
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-3", "checker"))
        now[0] = 52.0
        await feed_events(session, pilot, AgentExited(agent_id="agent-3", reason="crashed"))
        rule = ("agent-3 crashed · 42s", "error_rule")
        assert texts(app, "agent-3")[-1] == rule
        assert texts(app, None)[-1] == rule
        assert tab_strip(app) == STRIP


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


class TranscriptApp(App[None]):
    """The conversation window alone, its spinners stopped."""

    def compose(self) -> ComposeResult:
        transcript = Transcript()
        transcript.clock = lambda: 0.0
        yield transcript


def line(text: str) -> Entry:
    """agent-1's message ``text``, as it thinks out loud in a turn."""
    return Entry(text, "assistant", "agent-1", "t", speaker="agent-1")


async def lined(pilot: Pilot[None], transcript: Transcript, *texts: str) -> TranscriptLog:
    """The shown log, once agent-1's lines ``texts`` are added under All and laid out."""
    for text in texts:
        transcript.conversation.add_entry(line(text))
    await pilot.pause()
    return transcript.shown


FORTY = [f"line {i}" for i in range(40)]


async def test_the_shown_log_stays_pinned_to_the_latest_entry_unless_it_has_a_cursor() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        log = await lined(pilot, transcript, *FORTY)
        assert log.max_scroll_y > 0 and await scrolled_to_end(pilot, log)
        transcript.enter_list_mode()
        await pilot.pause()
        assert log.highlighted == 39
        await pilot.press("home")
        assert log.highlighted == 0 and log.scroll_y == 0
        await lined(pilot, transcript, "later")
        assert log.scroll_y == 0 and log.highlighted == 0
        await pilot.press("escape")
        transcript.release_cursor()
        await lined(pilot, transcript, "latest")
        assert await scrolled_to_end(pilot, log)


async def test_escape_holds_the_cursor_and_the_conversation_stops_following() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        log = await lined(pilot, transcript, *FORTY)
        transcript.enter_list_mode()
        await pilot.press("home", "j", "escape")
        assert app.focused is None and log.highlighted == 1
        await lined(pilot, transcript, "later")
        assert log.scroll_y == 0 and log.highlighted == 1
        assert entry(log, 1).gutter.strip() == GUTTER_MARKER


async def test_switching_tabs_shows_the_end_of_a_tab_with_no_cursor() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        await lined(pilot, transcript, *FORTY)
        transcript.show_only(PLANNER, PLANNER)
        await pilot.pause()
        transcript.show_only(None)  # Back to All, whose log is laid out again at its end.
        log = transcript.shown
        assert await scrolled_to_end(pilot, log)
        assert log.max_scroll_y > 0


def displayed_logs(app: App[None]) -> list[TranscriptLog]:
    """The transcript's logs that are displayed."""
    return [
        log for log in app.query("Transcript > TranscriptLog").results(TranscriptLog) if log.display
    ]


def marked_shown(app: App[None]) -> list[TranscriptLog]:
    """The transcript's logs marked shown."""
    return list(app.query("Transcript > TranscriptLog.-shown").results(TranscriptLog))


async def test_switching_tabs_displays_only_the_shown_log() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(120, 30)) as pilot:
        transcript = app.query_one(Transcript)
        transcript.conversation.add_entry(line("first"), "coder")
        everything = transcript.shown
        assert displayed_logs(app) == marked_shown(app) == [everything]
        transcript.show_only("coder", "coder")
        await pilot.pause()
        coder = transcript.shown
        assert transcript.only == "coder" and coder is not everything
        assert displayed_logs(app) == marked_shown(app) == [coder]
        transcript.show_only(None)
        await pilot.pause()
        assert transcript.shown is everything
        assert displayed_logs(app) == marked_shown(app) == [everything]
        transcript.show_only("coder", "● coder")
        transcript.show_only("coder", "coder")
        await pilot.pause()
        assert displayed_logs(app) == marked_shown(app) == [coder]


async def scrolled_to_top(pilot: Pilot[None], log: TranscriptLog) -> bool:
    """Whether ``log`` reaches its top, waiting out a scroll that lags a refresh."""
    for _ in range(100):
        if log.scroll_y == 0:
            return True
        await pilot.pause()
    return False


async def held_out_of_view(pilot: Pilot[None], transcript: Transcript) -> TranscriptLog:
    """The shown log, holding its first entry with the view scrolled away to the end."""
    log = await lined(pilot, transcript, *FORTY)
    transcript.enter_list_mode()
    await pilot.press("home", "escape")
    log.scroll_end(animate=False)
    await pilot.pause()
    assert log.scroll_y > 0
    return log


async def test_switching_back_scrolls_to_a_held_entry_out_of_view() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        log = await held_out_of_view(pilot, transcript)
        transcript.show_only(PLANNER, PLANNER)
        transcript.show_only(None)
        assert await scrolled_to_top(pilot, log) and log.highlighted == 0


async def test_show_place_scrolls_back_to_a_held_entry() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        log = await held_out_of_view(pilot, app.query_one(Transcript))
        log.show_place()
        assert await scrolled_to_top(pilot, log) and log.highlighted == 0


async def test_switching_tabs_keeps_a_held_place() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        log = await lined(pilot, transcript, *FORTY)
        transcript.enter_list_mode()
        await pilot.press("home")
        transcript.show_only(PLANNER, PLANNER)  # Away while in list mode.
        await pilot.pause()
        transcript.show_only(None)
        await pilot.pause()
        assert app.focused is None and log.highlighted == 0
        for _ in range(5):
            await pilot.pause()
        assert log.scroll_y == 0 and log.max_scroll_y > 0


async def test_a_running_tool_call_ticking_on_a_hidden_tab_is_measured_once_shown() -> None:
    app = TranscriptApp()
    async with app.run_test(size=(120, 30)) as pilot:
        transcript = app.query_one(Transcript)
        conversation = transcript.conversation
        conversation.add_entry(Entry("▶ agent-1 (coder) started", "agent", "agent-1"), "coder")
        conversation.add_entry(line(LONG), "coder")
        transcript.show_only("coder", "coder")  # Its log is built and shown.
        await pilot.pause()
        coder = transcript.shown
        transcript.show_only(None)  # coder's log is hidden.
        await pilot.pause()
        conversation.add_tool_call(call("agent-1", "c1", path=LONG), "coder", "agent-1")
        transcript.clock = lambda: 1 / SPINNER_FPS
        transcript.tick()
        transcript.show_only("coder", "coder")
        await pilot.pause()
        await pilot.pause()
        shown = coder.row_lines()
        coder._line_cache.clear()
        assert shown == coder.row_lines()
