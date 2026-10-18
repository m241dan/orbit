"""The rich renderables that draw a transcript entry: markdown with left headings,
a hanging indent and the gutter beside it."""

from dataclasses import dataclass
from typing import ClassVar

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import Heading, Markdown, MarkdownContext, MarkdownElement
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from orbit.tui.view.common import EntryKind
from orbit.tui.view.transcript import hanging_indent


class LeftHeading(Heading):
    """A heading drawn bold and left-aligned, h1 also underlined, rather than centred."""

    def on_enter(self, context: MarkdownContext) -> None:
        self.text = Text()
        context.enter_style(Style(bold=True, underline=self.tag == "h1"))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        yield text


class EntryMarkdown(Markdown):
    """Markdown as an entry shows it: headings drawn as :class:`LeftHeading`."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": LeftHeading,
    }


def entry_markdown(text: str, style: Style) -> Markdown:
    """``text`` as markdown in the entry's ``style``, its links shown as ``text (url)``."""
    return EntryMarkdown(text, style=style, hyperlinks=False)


@dataclass(frozen=True)
class HangingIndent:
    """``body`` drawn in the width left beside its indent: ``first`` in front of its first row
    and ``rest`` in front of every row after it, both in ``style``. A body that renders no rows,
    such as markdown of only whitespace, still gets a row holding ``first``."""

    body: RenderableType
    first: str
    rest: str
    style: Style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.first)
        rows = console.render_lines(self.body, options.update_width(width), pad=False)
        for index, row in enumerate(rows or [[]]):
            yield Segment(self.rest if index else self.first, self.style)
            yield from row
            yield Segment.line()


@dataclass(frozen=True)
class GutteredEntry:
    """A transcript entry as drawn: its ``heading`` if it has one, then its ``body``, or its
    ``label`` when it has none, wrapped to the width left beside the ``gutter``, which goes in
    front of every row in ``gutter_style``, then a blank row so the entry reads as a
    paragraph. With a ``band``, every row is filled to its end in that style."""

    label: Text
    gutter: str
    gutter_style: Style
    body: RenderableType | None = None
    heading: Text | None = None
    band: Style | None = None

    def _rows(self, console: Console, options: ConsoleOptions) -> list[list[Segment]]:
        """The rows of the entry's heading and body, drawn in ``options``' width."""
        shown = self.label if self.body is None else self.body
        parts = (shown,) if self.heading is None else (self.heading, shown)
        return [
            row
            for part in parts
            for row in console.render_lines(
                part, options, style=self.band, pad=self.band is not None
            )
        ]

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width - len(self.gutter)
        rows = self._rows(console, options.update_width(width))
        for row in rows:
            yield Segment(self.gutter, self.gutter_style)
            yield from row
            yield Segment.line()
        yield Segment.line()


def markdown_body(kind: EntryKind, text: str, style: Style) -> HangingIndent:
    """``text`` drawn as markdown in ``style``, the prefix and marker of an entry of ``kind``
    hanging beside it if it has them."""
    first, rest = hanging_indent(kind, text)
    return HangingIndent(entry_markdown(text, style), first, rest, style)
