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
from orbit.tui.view.tool_calls import tool_call_started
from orbit.tui.view.transcript import GUTTER_BAR, GUTTER_MARKER, PROMPT_LINES_SHOWN, PROMPT_MARKER
from tests.tui_fakes import (
    LONG,
    call,
    entry,
    entry_lines,
    entry_rows,
    label_style,
    result,
    scrolled_to_end,
)


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


def test_a_log_built_hidden_is_not_displayed() -> None:
    assert not TranscriptLog().display
    assert TranscriptLog(shown=True).display


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


async def test_a_prompt_that_renders_nothing_still_draws_its_marker_in_its_band() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        log.append(Entry("<!-- x -->", "prompt"))
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


async def test_replacing_a_rule_in_a_log_without_one_leaves_it() -> None:
    app = LogApp()
    async with app.run_test():
        log = app.query_one(TranscriptLog)
        log.append(Entry("hi", "agent", "agent-3"))
        log.replace_rule("agent-3", "agent-3 stopped", "rule")
        assert log.entries == (Entry("hi", "agent", "agent-3"),)


async def test_dropping_an_entry_keeps_the_cursor_on_its_own_entry() -> None:
    app = LogApp()
    async with app.run_test() as pilot:
        log = app.query_one(TranscriptLog)
        first, second, third = (Entry(text, "agent") for text in ("a", "b", "c"))
        for added in (first, second, third):
            log.append(added)
        log.highlighted = 2
        log.drop(first)
        await pilot.pause()
        assert log.entries == (second, third)
        assert log.option_count == 2
        assert log.highlighted == 1


async def test_dropping_the_held_entry_moves_the_cursor_to_the_one_before() -> None:
    app = LogApp()
    async with app.run_test():
        log = app.query_one(TranscriptLog)
        first, second = Entry("a", "agent"), Entry("b", "agent")
        log.append(first)
        log.append(second)
        log.highlighted = 1
        log.drop(second)
        assert log.entries == (first,)
        assert log.highlighted == 0


async def test_a_call_after_a_dropped_entry_still_finishes() -> None:
    app = LogApp()
    async with app.run_test():
        log = app.query_one(TranscriptLog)
        removed = Entry("hi", "prompt")
        log.append(removed)
        log.append_call(tool_call_started(call("agent-1", "c1")), "agent-1", None)
        log.drop(removed)
        assert log.finish_call(result("agent-1", "c1", "done"))
        [finished] = log.entries
        assert finished.call is not None and finished.call.status != "running"
        assert not log.running


async def test_dropping_an_entry_the_log_lacks_leaves_it() -> None:
    app = LogApp()
    async with app.run_test():
        log = app.query_one(TranscriptLog)
        log.append(Entry("hi", "prompt"))
        log.drop(Entry("hi", "prompt"))  # Equal, but not the one added.
        assert log.entries == (Entry("hi", "prompt"),)
