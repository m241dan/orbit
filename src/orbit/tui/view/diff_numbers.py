"""Best-effort line numbers for the diff of an edit, found in the snippet of the edited file
that its result shows; never a number that may be wrong."""

import re
from collections.abc import Iterator, Sequence

from orbit.tui.view.common import text_lines
from orbit.tui.view.diffs import EDIT_SEPARATOR

NUMBERED_LINE = re.compile(r" *(\d+)(?:→|\t)(.*)")
"""A line of Claude Code's numbered output: its line number, an arrow or a tab, then the line."""

type Snippet = Sequence[tuple[int, str]]


def edited_snippet(output: str) -> list[tuple[int, str]]:
    """The numbered lines of the edited file that an edit's ``output`` shows, with their
    numbers."""
    matches = (NUMBERED_LINE.fullmatch(line) for line in text_lines(output))
    return [(int(match[1]), match[2]) for match in matches if match is not None]


def diff_numbers(name: str, lines: Sequence[str], output: str) -> list[int | None]:
    """The new file's line number of each of ``lines``, the diff a call to the tool ``name``
    made, as its ``output`` shows them; None for removed lines and those it can't be sure
    of."""
    if name == "Write":
        return _numbered(lines, 1)
    snippet = edited_snippet(output)
    numbers: list[int | None] = []
    for index, edit in enumerate(_edits(lines)):
        numbers += [None] if index else []
        numbers += _numbered(edit, _start(snippet, [line[1:] for line in edit if line[:1] != "-"]))
    return numbers


def _edits(lines: Sequence[str]) -> Iterator[list[str]]:
    """The diffs of each edit in ``lines``, split at their separators."""
    edit: list[str] = []
    for line in lines:
        if line == EDIT_SEPARATOR:
            yield edit
            edit = []
        else:
            edit.append(line)
    yield edit


def _start(snippet: Snippet, new: Sequence[str]) -> int | None:
    """The line number at which the ``new`` lines fit in ``snippet``, if they do in just one
    place."""
    starts = [number for index, (number, _) in enumerate(snippet) if _found(snippet[index:], new)]
    return starts[0] if new and len(starts) == 1 else None


def _found(snippet: Snippet, new: Sequence[str]) -> bool:
    """Whether the ``new`` lines fit the start of ``snippet``, numbered one after another."""
    if len(snippet) < len(new):
        return False
    first = snippet[0][0]
    return all(
        number == first + offset and _fits(line, offset, new)
        for offset, (number, line) in enumerate(snippet[: len(new)])
    )


def _fits(line: str, offset: int, new: Sequence[str]) -> bool:
    """Whether the ``new`` line at ``offset`` could be in the file's ``line``: an edit's text
    may start and end mid-line, so the first may be a line's tail and the last its head."""
    part = new[offset]
    if len(new) == 1:
        return part in line
    if offset == 0:
        return line.endswith(part)
    if offset == len(new) - 1:
        return line.startswith(part)
    return line == part


def _numbered(lines: Sequence[str], start: int | None) -> list[int | None]:
    """The numbers of ``lines`` counting the unremoved ones from ``start``; all None without
    it."""
    numbers: list[int | None] = []
    for line in lines:
        if start is None or line[:1] == "-":
            numbers.append(None)
            continue
        numbers.append(start)
        start += 1
    return numbers


def numbered_diff(lines: Sequence[str], numbers: Sequence[int | None]) -> tuple[list[str], int]:
    """``lines`` after a column of their ``numbers``, right-aligned, and the column's width
    with its space; ``lines`` as they are, width 0, when none has a number."""
    labels = [_label(number) for number in numbers]
    width = max(map(len, labels), default=0)
    if not width:
        return list(lines), 0
    numbered = [f"{label:>{width}} {line}" for label, line in zip(labels, lines, strict=True)]
    return numbered, width + 1


def _label(number: int | None) -> str:
    return "" if number is None else str(number)
