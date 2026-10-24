"""A transcript log shows one tab's entries, each drawn beside its gutter, collapsed or
expanded."""

import pytest
from rich.color import Color
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult

from orbit.tui.entries import GutteredEntry
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.common import EntryKind
from orbit.tui.view.log import Entry
from orbit.tui.view.transcript import GUTTER_BAR, GUTTER_MARKER, PROMPT_MARKER
from tests.tui_fakes import (
    LONG,
    entry,
    entry_lines,
    entry_rows,
    label_style,
    scrolled_to_end,
)


async def test_entry_kinds_style_the_entries() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(Entry("Task 1: do it", "user"))
        log.entry_log.append(Entry("plain", "assistant"))
        await pilot.pause()
        header, plain = (label_style(entry(log, index)) for index in range(2))
        assert header == log.get_component_rich_style("transcript--user", partial=True)
        assert isinstance(header, Style) and header.bold
        assert plain == log.get_component_rich_style("transcript--assistant", partial=True)
        assert header.bgcolor is None


def test_a_log_built_hidden_is_not_displayed() -> None:
    assert not TranscriptLog().display
    assert TranscriptLog(shown=True).display


class LongLogApp(App[None]):
    """A shown log of 1,000 entries of two lines each, the ``tool`` ones collapsed."""

    def compose(self) -> ComposeResult:
        log = TranscriptLog(shown=True)
        for i in range(1000):
            log.entry_log.append(Entry(f"entry {i}\nmore", "tool" if i % 2 else "agent"))
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
        log.entry_log.toggle(995)
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
        log.entry_log.append(Entry(text, "assistant"))
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
        log.entry_log.append(Entry(text, "assistant"))
        await pilot.pause()
        assert entry_rows(log, 0)[0].startswith(GUTTER_BAR)
        assert entry_rows(log, 0)[-1] == ""
        log.focus()
        log.highlighted = 0
        await pilot.pause()
        assert entry_rows(log, 0)[0].startswith(GUTTER_MARKER)


async def test_a_prompt_that_renders_nothing_still_draws_its_marker_in_its_band() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(Entry("<!-- x -->", "prompt"))
        await pilot.pause()
        assert entry_rows(log, 0)[0].startswith(f"{GUTTER_BAR} {PROMPT_MARKER}")
        band = log.get_component_rich_style("transcript--prompt")
        line = entry_lines(log, 0)[0]
        gutter, *rest = backgrounds(line)
        assert gutter is None and set(rest) == {band.bgcolor}
        assert Segment.get_line_length(line) == log.size.width


async def appended(*entries: tuple[str, EntryKind]) -> list[list[str]]:
    """The rows of each of ``entries`` appended to a log, as drawn."""
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        for text, kind in entries:
            log.entry_log.append(Entry(text, kind))
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
        log.entry_log.append(Entry(f"{LONG}\n\nend", "prompt"))
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
        log.entry_log.append(Entry("Task 3: **do** it", "user"))
        await pilot.pause()
        header = entry(log, 0)
        assert header.band is None and header.body is None
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ Task 3: **do** it", ""]
        assert label_style(header).bold


def prompt_of(lines: int) -> str:
    return "\n".join(f"line {i}" for i in range(lines))


async def test_a_prompt_toggles_between_collapsed_and_expanded() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(Entry("one\n\ntwo", "prompt"))
        await pilot.pause()
        log.entry_log.toggle(0)
        await pilot.pause()
        assert log.collapsed(0)
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ ❯ ▸ one (+2 lines)", ""]
        log.entry_log.toggle(0)
        await pilot.pause()
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ ❯ ▾ one", "│", "│     two", ""]


@pytest.mark.parametrize("kind", ["user", "tool", "verify", "agent", "request", "error"])
async def test_entries_other_than_agents_messages_show_their_text_as_it_is(
    kind: EntryKind,
) -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(Entry("# **a**", kind))
        log.entry_log.append(Entry("# **a**\n- b", kind))
        await pilot.pause()
        if log.collapsed(1):
            log.entry_log.toggle(1)
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ # **a**", ""]
        assert [row.rstrip() for row in entry_rows(log, 1)] == ["│ ▾ # **a**", "│ - b", ""]


def rendered(log: TranscriptLog, rows: int) -> list[str]:
    """The text of the log's first ``rows`` rows as drawn."""
    return [log.render_line(y).text.rstrip() for y in range(rows)]


def said(text: str, turn: str | None = None) -> Entry:
    """agent-1's message ``text``, thinking out loud in ``turn`` if it's given."""
    return Entry(text, "assistant", "agent-1", turn, speaker="agent-1")


STARTED = Entry("▶ agent-1 (coder) started", "agent", "agent-1", speaker="agent-1")


async def test_entries_are_shown_as_paragraphs_separated_by_blank_lines() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(STARTED)
        log.entry_log.append(said("hi\n\nthere", "t"))
        await pilot.pause()
        assert rendered(log, 6) == [
            "│ ▶ agent-1 (coder) started",
            "",
            "│ ▾ hi",
            "│",
            "│   there",
            "",
        ]


async def test_the_gutter_is_a_dim_bar_and_the_cursors_entry_takes_the_marker() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(STARTED)
        log.entry_log.append(said("hi\nthere", "t"))
        await pilot.pause()
        bar = log.get_component_rich_style("transcript--gutter", partial=True)
        assert [entry(log, i).gutter_style for i in range(2)] == [bar, bar]
        log.highlighted = 1
        await pilot.pause()
        assert [entry(log, i).gutter.strip() for i in range(2)] == [GUTTER_BAR, GUTTER_MARKER]
        marker = log.get_component_rich_style("transcript--marker", partial=True)
        assert entry(log, 1).gutter_style == marker


def assert_rows_hold_label(log: TranscriptLog, rows: int, label: str) -> None:
    """Assert the log's first ``rows`` rows fit its content width and, past their gutter, hold
    all of ``label``, so wrapping beside the gutter crops nothing."""
    lines = rendered(log, rows)
    assert all(len(line) <= log.scrollable_content_region.width for line in lines)
    assert " ".join(line[2:].rstrip() for line in lines) == label


async def test_every_wrapped_row_of_an_entry_keeps_its_gutter() -> None:
    app = LogApp()
    async with app.run_test(size=(40, 24)) as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(Entry(LONG, "assistant", turn="t"))
        await pilot.pause()
        rows = log.virtual_size.height
        label = entry(log, 0).label.plain
        assert rows > 3  # The entry wraps onto several rows, then a blank one.
        assert all(row.startswith(f"{GUTTER_BAR} ") for row in rendered(log, rows - 1))
        assert rendered(log, rows)[-1] == ""
        assert_rows_hold_label(log, rows - 1, label)
        log.enter_list_mode()
        await pilot.pause()
        assert all(row.startswith(f"{GUTTER_MARKER} ") for row in rendered(log, rows - 1))
        assert rendered(log, rows)[-1] == ""
        assert_rows_hold_label(log, rows - 1, label)


async def test_the_cursors_entry_has_no_highlight_background() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(STARTED)
        log.entry_log.append(said("hi"))
        log.enter_list_mode()
        await pilot.pause()
        plain = log.get_visual_style("option-list--option")
        for focused in (True, False):
            assert (app.focused is log) == focused
            for state in ("option-list--option-highlighted", "option-list--option-hover"):
                assert log.get_visual_style("option-list--option", state) == plain
            await pilot.press("escape")


async def test_lines_are_wrapped_again_for_a_new_window_size() -> None:
    app = LogApp()
    async with app.run_test(size=(60, 12)) as pilot:
        log = app.query_one(TranscriptLog)
        for _ in range(6):
            log.entry_log.append(said(LONG, "t"))
        await pilot.pause()
        narrow = log.max_scroll_y
        await pilot.resize_terminal(160, 12)
        await pilot.pause()
        assert log.max_scroll_y < narrow


async def drawn(*entries: Entry) -> list[list[str]]:
    """The rows of each of ``entries`` appended to a log, as drawn, without trailing space."""
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        for added in entries:
            log.entry_log.append(added)
        await pilot.pause()
        return [[row.rstrip() for row in entry_rows(log, i)] for i in range(len(entries))]


async def test_an_agents_message_of_several_lines_hangs_its_marker_beside_its_markdown() -> None:
    (rows,) = await drawn(said("**intro**\n\n- one\n- two", "t"))
    assert rows == ["│ agent-1", "│ ▾ intro", "│", "│    • one", "│    • two", ""]


async def test_an_agents_message_wraps_beside_its_hanging_marker() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(said(f"{LONG}\n\nend", "t"))
        await pilot.pause()
        rows = entry_rows(log, 0)
        assert rows[0] == f"{GUTTER_BAR} agent-1"
        assert rows[1].startswith(f"{GUTTER_BAR} ▾ gutter")
        assert all(row.startswith(f"{GUTTER_BAR}   ") for row in rows[2:-1])
        words = " ".join(row[4:].strip() for row in rows[1:-1]).split()
        assert words == [*LONG.split(), "end"]


async def test_a_collapsed_agents_message_shows_its_first_line_as_it_is() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(said("**intro**\n\n- one", "t"))
        log.entry_log.toggle(0)
        await pilot.pause()
        assert entry(log, 0).body is None
        assert [row.rstrip() for row in entry_rows(log, 0)] == [
            "│ agent-1",
            "│ ▸ **intro** (+2 lines)",
            "",
        ]


async def test_an_agents_message_of_one_line_has_no_marker() -> None:
    (rows,) = await drawn(said("**hi** `there`", "t"))
    assert rows == ["│ agent-1", "│ hi there", ""]


async def test_an_agents_message_is_headed_by_a_muted_label_row_and_drawn_dim() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(said("hi", "t"))
        await pilot.pause()
        heading = entry(log, 0).heading
        assert heading is not None
        label = log.get_component_rich_style("transcript--label", partial=True)
        assert heading.get_style_at_offset(Console(), 0) == label
        assert [row.rstrip() for row in entry_rows(log, 0)] == ["│ agent-1", "│ hi", ""]
        thinking = log.get_component_rich_style("transcript--thinking", partial=True)
        assert label_style(entry(log, 0)) == thinking
        assert thinking != log.get_component_rich_style("transcript--assistant", partial=True)


async def test_the_label_row_takes_the_cursors_marker() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(said("hi", "t"))
        log.enter_list_mode()
        await pilot.pause()
        rows = entry_rows(log, 0)
        assert [row.rstrip() for row in rows] == ["▌ agent-1", "▌ hi", ""]


@pytest.mark.parametrize(
    ("text", "kind"), [("agent-3 done", "rule"), ("agent-3 error", "error_rule")]
)
async def test_a_rule_is_drawn_across_the_width_in_its_style(text: str, kind: EntryKind) -> None:
    app = LogApp()
    async with app.run_test(size=(60, 20)) as pilot:
        log = app.query_one(TranscriptLog)
        log.entry_log.append(Entry(text, kind, "agent-3", speaker="agent-3"))
        await pilot.pause()
        style = log.get_component_rich_style(f"transcript--{kind}", partial=True)
        assert label_style(entry(log, 0)) == style
        row = entry_rows(log, 0)[0]
        assert row.startswith(f"{GUTTER_BAR} ──")
        assert row.endswith("──") and len(row) == log.size.width
        segments = [s for s in entry_lines(log, 0)[0] if s.text.strip("─ ")]
        assert all(segment.style == style for segment in segments[1:])


async def test_rules_and_errors_have_their_own_styles() -> None:
    app = LogApp()
    async with app.run_test():
        log = app.query_one(TranscriptLog)
        rule = log.get_component_rich_style("transcript--rule", partial=True)
        error_rule = log.get_component_rich_style("transcript--error_rule", partial=True)
        assert rule == log.get_component_rich_style("transcript--tool", partial=True)
        assert error_rule == log.get_component_rich_style("transcript--error", partial=True)
