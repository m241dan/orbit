"""What ``orbit init --update`` adds to a project's ``.orbit/config.toml`` from the reference
config: each setting the reference has that the file doesn't mention, set or commented out,
without changing, moving or removing any of the file's lines."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, auto
from itertools import chain
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from orbit.agent.domain.config_errors import ConfigError

ADDED_NOTE = "# Added by orbit init --update on {date}: settings this file didn't mention."
"""The line the tables a file lacks are appended under, with the day they're added."""

_HEADER = re.compile(r"\s*(?:#\s*)?(\[\[?)\s*([\w\-.\"' ]+?)\s*\]\]?\s*(?:#.*)?")
_COMMENTED_KEY = re.compile(r"\s*#\s*([\w-]+)\s*[=:]")
_QUOTES = ('"""', "'''")


@dataclass(frozen=True)
class Additions:
    """What an update added to a file: ``keys``, by the header of the table they were added to
    (empty for the top level), and the ``tables`` appended whole, by header."""

    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigUpdate:
    """A file's ``text`` once updated, and the ``additions`` that made it."""

    text: str
    additions: Additions


class _Kind(Enum):
    """What a line of a TOML file is, as an update reads it."""

    BLANK = auto()
    HEADER = auto()  # A table's header, set or commented out.
    KEY = auto()  # A key commented out, as "# key = value" or "# key: unset".
    COMMENT = auto()
    SETTING = auto()  # A key set, or any line of a value.


@dataclass(frozen=True)
class _Line:
    """A line's ``text``, its ``kind``, and the header or key it names, if it's one."""

    text: str
    kind: _Kind
    name: str = ""


@dataclass(frozen=True)
class _Region:
    """The lines of the table under ``header``, empty for the top level: from ``start``, the
    first of its header and the comments just above it, to ``end``, as :func:`_end` places
    it."""

    header: str
    start: int
    end: int

    @property
    def table(self) -> str:
        """The table's dotted name, empty for the top level."""
        return self.header.strip("[]")


@dataclass(frozen=True)
class _Insertion:
    """The ``keys`` added to the table under ``header``, as ``lines``, before the line ``at``."""

    at: int
    header: str
    keys: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _File:
    """A file as an update reads it: what TOML reads in it (``data``), its ``lines``, and the
    ``regions`` of its tables, set or commented out."""

    data: dict[str, Any]
    lines: list[_Line]
    regions: list[_Region]

    def has(self, table: str) -> bool:
        """Whether the file has ``table``, set or under a commented header."""
        return _set_keys(self.data, table) is not None or self.end(table) is not None

    def mentions(self, table: str) -> set[str]:
        """The keys of ``table`` the file sets or has commented out under its header."""
        commented = {line.name for line in self._lines_of(table) if line.kind is _Kind.KEY}
        return commented | (_set_keys(self.data, table) or set())

    def end(self, table: str) -> int | None:
        """Where keys added to ``table`` go: the end of its last region; none if it has none,
        being set only inline or by dotted keys."""
        ends = [region.end for region in self.regions if region.table == table]
        return ends[-1] if ends else None

    def _lines_of(self, table: str) -> list[_Line]:
        """The lines of ``table``'s regions."""
        return [
            line
            for region in self.regions
            if region.table == table
            for line in self.lines[region.start : region.end]
        ]


def update_text(text: str, reference: str, today: date) -> ConfigUpdate:
    """``text``, a project file, with what ``reference`` has that it doesn't mention, as
    ``orbit init --update`` adds it on ``today``.

    A table counts as there if it's set or its header is commented out, and a key if it's set
    or commented out under its table, as ``# key = …`` or ``# key: …``. A key missing from a
    table that's there is added, under the comments describing it in ``reference``, after the
    table's last line; each whole table missing is appended at the end, as ``reference`` has
    it, all under one :data:`ADDED_NOTE`. Every line of ``text`` stays as it was, in order.

    Raises :class:`ConfigError` if ``text`` isn't valid TOML.
    """
    file = _read(text)
    lines = _scanned(reference)
    regions = _regions(lines)
    insertions = _insertions(file, lines, regions)
    appended = [region for region in regions if not file.has(region.table)]
    if not insertions and not appended:
        return ConfigUpdate(text, Additions())
    updated = _appended(_inserted(file.lines, insertions), lines, appended, today)
    return ConfigUpdate("\n".join(updated) + "\n", _additions(insertions, appended))


def _read(text: str) -> _File:
    """``text`` as an update reads it. Raises :class:`ConfigError` if it isn't valid TOML."""
    try:
        data = tomlkit.parse(text).unwrap()
    except TOMLKitError as exc:
        raise ConfigError(str(exc)) from None
    lines = _scanned(text)
    return _File(data, lines, _regions(lines))


def _set_keys(data: dict[str, Any], table: str) -> set[str] | None:
    """The keys ``data`` sets in ``table``, in any of its tables if it's an array of them; none
    if ``data`` sets nothing there."""
    value: Any = data
    for part in table.split(".") if table else ():
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return _keys_of(value)


def _keys_of(value: Any) -> set[str]:
    """The keys of ``value``, a table, or of each table in it, an array."""
    tables = value if isinstance(value, list) else [value]
    return {key for table in tables if isinstance(table, dict) for key in table}


def _scanned(text: str) -> list[_Line]:
    """The lines of ``text``, each with its kind; those inside a multi-line string are its
    value."""
    lines: list[_Line] = []
    quotes: str | None = None
    for line_text in text.removesuffix("\n").split("\n") if text else ():
        if quotes is None:
            line = _line(line_text)
            quotes = _opened(line)
        else:
            line = _Line(line_text, _Kind.SETTING)
            quotes = None if line_text.count(quotes) % 2 else quotes
        lines.append(line)
    return lines


def _line(text: str) -> _Line:
    """The line ``text``, outside any multi-line string."""
    if not text.strip():
        return _Line(text, _Kind.BLANK)
    if header := _HEADER.fullmatch(text):
        return _Line(text, _Kind.HEADER, _header(header))
    if key := _COMMENTED_KEY.match(text):
        return _Line(text, _Kind.KEY, key[1])
    return _Line(text, _Kind.COMMENT if text.lstrip().startswith("#") else _Kind.SETTING)


def _header(match: re.Match[str]) -> str:
    """The header ``match`` matched, as ``[name]``, or ``[[name]]`` for an array of tables,
    without its spacing, quotes or comment."""
    brackets = match[1]
    name = ".".join(part.strip().strip("\"'") for part in match[2].split("."))
    return f"{brackets}{name}{brackets.replace('[', ']')}"


def _opened(line: _Line) -> str | None:
    """The quotes of the multi-line string ``line`` leaves open, if it does."""
    if line.kind is not _Kind.SETTING:
        return None
    return next((quotes for quotes in _QUOTES if line.text.count(quotes) % 2), None)


def _regions(lines: Sequence[_Line]) -> list[_Region]:
    """The top level's region of ``lines``, then each table's, in order."""
    headers = [index for index, line in enumerate(lines) if line.kind is _Kind.HEADER]
    starts = [0, *(_comments_above(lines, index) for index in headers)]
    bounds = zip(starts, [*starts[1:], len(lines)], strict=True)
    names = ["", *(lines[index].name for index in headers)]
    return [
        _Region(name, start, _end(lines, start, stop))
        for name, (start, stop) in zip(names, bounds, strict=True)
    ]


def _comments_above(lines: Sequence[_Line], index: int) -> int:
    """The first of the comments just above the line at ``index``, or ``index`` if there are
    none."""
    start = index
    while start > 0 and lines[start - 1].kind is _Kind.COMMENT:
        start -= 1
    return start


def _end(lines: Sequence[_Line], start: int, stop: int) -> int:
    """Where the region of ``lines`` from ``start`` to ``stop`` ends: after its last header or
    setting, set or commented out, or the comments going on from it with no blank line between;
    comments at its ``start``, opening the file, count as going on from it."""
    end = start
    for index in range(start, stop):
        kind = lines[index].kind
        if kind is _Kind.BLANK:
            continue
        if kind is not _Kind.COMMENT or index == end:
            end = index + 1
    return end


def _insertions(
    file: _File, reference: Sequence[_Line], regions: Sequence[_Region]
) -> list[_Insertion]:
    """The keys of each of the ``regions`` of the ``reference`` that ``file`` doesn't mention,
    as :func:`_insertion` adds them."""
    return [found for region in regions if (found := _insertion(file, reference, region))]


def _insertion(file: _File, reference: Sequence[_Line], region: _Region) -> _Insertion | None:
    """The keys of ``region`` of the ``reference`` that ``file`` doesn't mention, added after
    the end of its table; none if there are none, or if the table has no region there."""
    at = file.end(region.table)
    if at is None:
        return None
    mentioned = file.mentions(region.table)
    missing = {
        key: lines for key, lines in _described(reference, region).items() if key not in mentioned
    }
    if not missing:
        return None
    lines = tuple(chain.from_iterable(missing.values()))
    return _Insertion(at, region.header, tuple(missing), lines)


def _described(lines: Sequence[_Line], region: _Region) -> dict[str, tuple[str, ...]]:
    """Each key commented out in ``region`` of ``lines``, with the comments just above it that
    describe it, then its own line."""
    return {
        lines[index].name: tuple(
            line.text for line in lines[_comments_above(lines, index) : index + 1]
        )
        for index in range(region.start, region.end)
        if lines[index].kind is _Kind.KEY
    }


def _inserted(lines: Sequence[_Line], insertions: Sequence[_Insertion]) -> list[str]:
    """The text of ``lines`` with each of ``insertions``' lines before the line it's at, apart
    from it by a blank line unless it's blank."""
    blocks = {insertion.at: insertion.lines for insertion in insertions}
    text: list[str] = []
    for index, line in enumerate(lines):
        if index in blocks:
            text += [*blocks[index], *([] if line.kind is _Kind.BLANK else [""])]
        text.append(line.text)
    return [*text, *blocks.get(len(lines), ())]


def _appended(
    text: list[str], reference: Sequence[_Line], regions: Sequence[_Region], today: date
) -> list[str]:
    """``text`` followed by the ``regions`` of the ``reference``, if there are any, under
    :data:`ADDED_NOTE` for ``today``, apart from ``text`` and from each other by a blank
    line."""
    if not regions:
        return text
    note = ADDED_NOTE.format(date=today.isoformat())
    sections = [
        line.text
        for region in regions
        for line in (_Line("", _Kind.BLANK), *reference[region.start : region.end])
    ]
    return [*text, *_apart(text), note, *sections]


def _apart(text: Sequence[str]) -> list[str]:
    """The blank line that keeps what follows ``text`` apart from it, unless ``text`` is empty
    or ends with one."""
    return [""] if text and text[-1].strip() else []


def _additions(insertions: Sequence[_Insertion], appended: Sequence[_Region]) -> Additions:
    """What ``insertions`` and the ``appended`` regions add to a file."""
    return Additions(
        {insertion.header: insertion.keys for insertion in insertions},
        tuple(region.header for region in appended),
    )
