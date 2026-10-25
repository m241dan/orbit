"""What ``orbit init --update`` adds to a project's ``.orbit/config.toml`` from the reference
config: each setting the reference has that the file doesn't mention, set or commented out; and
what ``orbit init --roles`` adds: a commented-out line pointing each built-in role at its copy of
its instructions. Neither changes, moves or removes any of the file's lines. Also the checks the
configurator writes: in place of a file's verification steps and setup, changing no other line,
or of the reference config's examples of them, when it starts a file from the reference."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum, auto
from itertools import chain, groupby, pairwise
from typing import Any

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Table

from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.config_errors import ConfigError

ADDED_NOTE = "# Added by orbit init --update on {date}: settings this file didn't mention."
"""The line the tables a file lacks are appended under, with the day they're added."""

FOR_EXAMPLE = "For example:"
"""What ends the line describing each of the reference config's examples, just above it."""

_STEPS = "[[verification.steps]]"
_SETUP = "[setup]"
_HEADER = re.compile(r"\s*(?:#\s*)?(\[\[?)\s*([\w\-.\"' ]+?)\s*\]\]?\s*(?:#.*)?")
_COMMENTED_KEY = re.compile(r"\s*#\s*([\w-]+)\s*[=:]")
_COMMENTED_VALUE = re.compile(r"\s*#\s*[\w-]+\s*=")
_LATER = "#later"  # The region a later version's heading starts, no table's name.
_QUOTES = ('"""', "'''")


@dataclass(frozen=True)
class Additions:
    """What an update added to a file: ``keys``, by the header of the table they were added to
    (empty for the top level), and what it appended at the end (``tables``), in order: each table
    appended whole, by header, and each key of a later version's appended on its own, by
    name."""

    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigUpdate:
    """A file's ``text`` once updated, and the ``additions`` that made it."""

    text: str
    additions: Additions


@dataclass(frozen=True)
class InstructionLines:
    """The built-in roles, by name, that ``orbit init --roles`` gave the line pointing at their
    copies of their instructions: in their tables, under a set header (``added``) or a
    commented-out one (``commented``), or with their tables appended whole, commented out
    (``appended``); and those lacking it that it gave none, their tables being set only inline
    or by dotted keys (``inline``)."""

    added: tuple[str, ...] = ()
    commented: tuple[str, ...] = ()
    appended: tuple[str, ...] = ()
    inline: tuple[str, ...] = ()

    @property
    def under_commented_headers(self) -> tuple[str, ...]:
        """The roles given the line under a commented-out header, which uncommenting the line
        alone wouldn't switch: those ``commented``, then those ``appended``."""
        return self.commented + self.appended


@dataclass(frozen=True)
class InstructionsUpdate:
    """A file's ``text`` once ``orbit init --roles`` added to it, and the ``lines`` it added."""

    text: str
    lines: InstructionLines


class _Placement(Enum):
    """Where ``orbit init --roles`` gives a role the line pointing it at its copy of its
    instructions."""

    ADDED = auto()  # In its table, under a set header.
    COMMENTED = auto()  # In its table, under a commented-out header.
    APPENDED = auto()  # With its table, appended commented out.
    INLINE = auto()  # Nowhere, its table having no header of its own.


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
class _Part:
    """What an update adds to a file from a part of the reference: the keys it adds to the
    file's tables (``insertions``), the ``sections`` it appends at the end, and what they add
    (``appended``): each table, by header, and each key appended on its own, by name."""

    insertions: list[_Insertion]
    sections: list[list[str]]
    appended: list[str]


@dataclass(frozen=True)
class _SetTable:
    """A table a file sets under a header of its own: its ``header``, from whose line,
    ``start``, it runs to after its last setting, ``end``."""

    header: str
    start: int
    end: int


@dataclass(frozen=True, order=True)
class _Span:
    """The lines from ``start`` to ``end``, and the ``lines`` that take their place."""

    start: int
    end: int
    lines: tuple[str, ...] = ()


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

    def gives(self, table: str, key: str) -> bool:
        """Whether the file sets ``key`` in ``table`` or has it commented out under its header
        with a value, as ``# key = …``, rather than a note such as ``# key: unset``."""
        if key in (_set_keys(self.data, table) or set()):
            return True
        return any(
            line.kind is _Kind.KEY and line.name == key and _COMMENTED_VALUE.match(line.text)
            for line in self._lines_of(table)
        )

    def mentions(self, table: str) -> set[str]:
        """The keys of ``table`` the file sets or has commented out under its header."""
        commented = {line.name for line in self._lines_of(table) if line.kind is _Kind.KEY}
        return commented | (_set_keys(self.data, table) or set())

    def mentions_anywhere(self, key: str) -> bool:
        """Whether the file sets ``key`` or has it commented out in any of its tables or at its
        top level."""
        return any(key in self.mentions(region.table) for region in self.regions)

    def end(self, table: str) -> int | None:
        """Where keys added to ``table`` go: the end of its last region; none if it has none,
        being set only inline or by dotted keys."""
        region = self._last_region(table)
        return None if region is None else region.end

    def role_region(self, table: str) -> _Region | None:
        """The region a role's line goes in for ``table``: its last under a set header if the
        file sets the table, none if it sets it only inline or by dotted keys; otherwise its
        last region, under a commented-out header, if it has one."""
        regions = self._regions_of(table)
        if _set_keys(self.data, table) is not None:
            regions = list(filter(self._set_header, regions))
        return regions[-1] if regions else None

    def _regions_of(self, table: str) -> list[_Region]:
        """``table``'s regions, in order."""
        return [region for region in self.regions if region.table == table]

    def _set_header(self, region: _Region) -> bool:
        """Whether ``region``'s header is set rather than commented out."""
        return not _commented_out(self.header_of(region))

    def header_of(self, region: _Region) -> _Line:
        """``region``'s header line."""
        lines = self.lines[region.start : region.end]
        return next(line for line in lines if line.kind is _Kind.HEADER)

    def _last_region(self, table: str) -> _Region | None:
        """``table``'s last region; none if it has none."""
        regions = [region for region in self.regions if region.table == table]
        return regions[-1] if regions else None

    def _lines_of(self, table: str) -> list[_Line]:
        """The lines of ``table``'s regions."""
        return [
            line
            for region in self.regions
            if region.table == table
            for line in self.lines[region.start : region.end]
        ]


def update_text(
    text: str, reference: str, today: date, later_heading: str | None = None
) -> ConfigUpdate:
    """``text``, a project file, with what ``reference`` has that it doesn't mention, as
    ``orbit init --update`` adds it on ``today``.

    A table counts as there if it's set or its header is commented out, and a key if it's set
    or commented out under its table, as ``# key = …`` or ``# key: …``. A key missing from a
    table that's there is added, under the comments describing it in ``reference``, after the
    table's last line; each whole table missing is appended at the end, as ``reference`` has
    it, all under one :data:`ADDED_NOTE`. Every line of ``text`` stays as it was, in order.

    With ``later_heading``, the settings of ``reference`` from the line commenting it out on are
    a later version's, belonging to no table before it. Those appended go after the others,
    under that line, and a key ahead of their tables is never added to a table: it's appended
    on its own unless ``text`` mentions it anywhere. In ``text`` too, a table's last line comes
    before the line commenting it out, so keys added to the table go above it.

    Raises :class:`ConfigError` if ``text`` isn't valid TOML.
    """
    file = _read(text, later_heading)
    current, later = _split(_scanned(reference), later_heading)
    part = _joined(_part(file, current, _regions(current)), _later_part(file, later))
    if not part.insertions and not part.sections:
        return ConfigUpdate(text, Additions())
    updated = _appended(_inserted(file.lines, part.insertions), part.sections, today)
    return ConfigUpdate("\n".join(updated) + "\n", _additions(part))


def instructions_copy(name: str) -> str:
    """Where ``orbit init --roles`` copies built-in role ``name``'s instructions, relative to the
    project file's directory."""
    return f"roles/{name}.md"


def instructions_setting(name: str) -> str:
    """The setting pointing built-in role ``name`` at its copy of its instructions (see
    :func:`instructions_copy`), as ``orbit init --roles`` adds it commented out."""
    return f'instructions = "{instructions_copy(name)}"'


def add_instructions(
    text: str,
    reference: str,
    roles: Sequence[str],
    today: date,
    later_heading: str | None = None,
) -> InstructionsUpdate:
    """``text``, a project file, with :func:`instructions_setting` commented out for each of the
    built-in ``roles`` whose table neither sets its instructions nor has them commented out with
    a value, as ``orbit init --roles`` adds it on ``today``.

    A table counts as there if it's set or its header is commented out, as for
    :func:`update_text`; the line follows its last line. Each role whose table isn't there gets
    the table appended at the end, as ``reference`` has it with the line after its last, all
    under one :data:`ADDED_NOTE`. With ``later_heading``, a table's last line comes before the
    line commenting it out, as for :func:`update_text`. A role whose table has no header of its
    own, being set only inline or by dotted keys, gets no line. Every line of ``text`` stays as
    it was, in order. The lines it reports say whether each role's line is under a set header or
    a commented-out one, the tables appended counting as commented out, and which roles got none.

    Raises :class:`ConfigError` if ``text`` isn't valid TOML.
    """
    file = _read(text, later_heading)
    lacking = _lacking_instructions(file, roles)
    insertions = _instruction_insertions(file, lacking)
    lines = _instruction_lines(file, lacking)
    if not insertions and not lines.appended:
        return InstructionsUpdate(text, lines)
    sections = _role_sections(_scanned(reference), lines.appended)
    updated = _appended(_inserted(file.lines, list(insertions.values())), sections, today)
    return InstructionsUpdate("\n".join(updated) + "\n", lines)


def with_checks(text: str, checks: ChecksProposal) -> str:
    """``text``, a project file, with ``checks`` as its verification steps and setup, as the
    configurator writes them.

    The file's ``[[verification.steps]]`` tables, from the first one's header to the last one's
    last setting, give way to the new ones, and its ``[setup]`` table, to its last setting, to
    the new one or, without one, goes with the blank lines just above it; those it lacks are
    appended at the end. Every other line stays as it was, in order, the comments after a
    table's last setting among them. Where that wouldn't read as replacing the steps and setup,
    as when the file sets them some other way, such as inline, it's rewritten as TOML instead,
    keeping its other keys and comments but those after a replaced table's last setting, which
    TOML counts as the table's.

    Raises :class:`ConfigError` if ``text`` isn't valid TOML.
    """
    try:
        rewritten = _rewritten(tomlkit.parse(text), checks)
    except TOMLKitError as exc:
        raise ConfigError(str(exc)) from None
    edited = _edited(_scanned(text), checks)
    return edited if _same_settings(edited, rewritten) else rewritten


def reference_with_checks(reference: str, checks: ChecksProposal) -> str:
    """``reference``, the reference config, with ``checks``' steps and setup in place of its
    commented-out examples of them: each example's header and keys give way to the tables,
    under the comments describing it, without the :data:`FOR_EXAMPLE` they end with. Without a
    setup, its example stays as it is."""
    lines = _scanned(reference)
    regions = {region.header: region for region in _regions(lines)}
    spans = [
        _example(lines, regions[header], tables)
        for header, tables in _check_tables(checks).items()
        if tables
    ]
    return "\n".join(_spliced(lines, spans)) + "\n"


def _read(text: str, heading: str | None = None) -> _File:
    """``text`` as an update reads it, the comment that's ``heading`` ending the region before
    it. Raises :class:`ConfigError` if it isn't valid TOML."""
    try:
        data = tomlkit.parse(text).unwrap()
    except TOMLKitError as exc:
        raise ConfigError(str(exc)) from None
    lines = _scanned(text)
    return _File(data, lines, _regions(lines, heading))


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


def _split(lines: list[_Line], heading: str | None) -> tuple[list[_Line], list[_Line]]:
    """``lines`` before the comment that's ``heading``, and those from it on; all of them, and
    none, without ``heading`` or such a comment."""
    if heading is None:
        return lines, []
    at = next(
        (index for index, line in enumerate(lines) if _is_heading(line, heading)),
        len(lines),
    )
    return lines[:at], lines[at:]


def _part(file: _File, lines: Sequence[_Line], regions: Sequence[_Region]) -> _Part:
    """What an update adds to ``file`` from the ``regions`` of ``lines``: the keys of each of
    their tables it has that it doesn't mention, and each table it lacks, whole."""
    appended = [region for region in regions if not file.has(region.table)]
    return _Part(
        _insertions(file, lines, regions),
        [_section(lines, region) for region in appended],
        [region.header for region in appended],
    )


def _later_part(file: _File, lines: Sequence[_Line]) -> _Part:
    """What an update adds to ``file`` from ``lines``, a later version's settings, the first
    being their heading: what :func:`_part` adds from their tables, after each key ahead of the
    tables that ``file`` doesn't mention anywhere, appended on its own as it belongs to no table;
    what's appended goes under the heading."""
    if not lines:
        return _Part([], [], [])
    heading, *settings = lines
    top, *tables = _regions(settings)
    keys = {
        key: list(described)
        for key, described in _described(settings, top).items()
        if not file.mentions_anywhere(key)
    }
    part = _part(file, settings, tables)
    sections = [*keys.values(), *part.sections]
    if not sections:
        return part
    return _Part(part.insertions, [[heading.text], *sections], [*keys, *part.appended])


def _joined(first: _Part, second: _Part) -> _Part:
    """What ``first`` adds, then what ``second`` does."""
    return _Part(
        first.insertions + second.insertions,
        first.sections + second.sections,
        first.appended + second.appended,
    )


def _regions(lines: Sequence[_Line], heading: str | None = None) -> list[_Region]:
    """The top level's region of ``lines``, then each table's, in order; with ``heading``, each
    comment that's it starts a region of no table, under :data:`_LATER`."""
    headers = [index for index, line in enumerate(lines) if _starts_region(line, heading)]
    starts = [0, *(_comments_above(lines, index) for index in headers)]
    bounds = zip(starts, [*starts[1:], len(lines)], strict=True)
    names = ["", *(_name(lines[index]) for index in headers)]
    return [
        _Region(name, start, _end(lines, start, stop))
        for name, (start, stop) in zip(names, bounds, strict=True)
    ]


def _name(line: _Line) -> str:
    """The name of the region ``line`` starts: its header's, or :data:`_LATER` for a heading."""
    return line.name or _LATER


def _starts_region(line: _Line, heading: str | None) -> bool:
    """Whether ``line`` is a header or the comment that's ``heading``."""
    return line.kind is _Kind.HEADER or (heading is not None and _is_heading(line, heading))


def _is_heading(line: _Line, heading: str) -> bool:
    """Whether ``line`` is the comment that's ``heading``."""
    return line.kind is _Kind.COMMENT and line.text == f"# {heading}"


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


def _section(lines: Sequence[_Line], region: _Region) -> list[str]:
    """The text of ``region`` of ``lines``."""
    return [line.text for line in lines[region.start : region.end]]


def _appended(text: list[str], sections: Sequence[Sequence[str]], today: date) -> list[str]:
    """``text`` followed by the lines of the ``sections``, if there are any, under
    :data:`ADDED_NOTE` for ``today``, apart from ``text`` and from each other by a blank
    line."""
    if not sections:
        return text
    note = ADDED_NOTE.format(date=today.isoformat())
    lines = chain.from_iterable(["", *section] for section in sections)
    return [*text, *_apart(text), note, *lines]


def _apart(text: Sequence[str]) -> list[str]:
    """The blank line that keeps what follows ``text`` apart from it, unless ``text`` is empty
    or ends with one."""
    return [""] if text and text[-1].strip() else []


def _additions(part: _Part) -> Additions:
    """What ``part`` adds to a file."""
    return Additions(
        {insertion.header: insertion.keys for insertion in part.insertions}, tuple(part.appended)
    )


def _role_table(name: str) -> str:
    """Role ``name``'s table's dotted name."""
    return f"roles.{name}"


def _lacking_instructions(file: _File, roles: Sequence[str]) -> list[str]:
    """The ``roles`` whose tables in ``file`` neither set their instructions nor have them
    commented out with a value."""
    return [name for name in roles if not file.gives(_role_table(name), "instructions")]


def _instruction_lines(file: _File, roles: Sequence[str]) -> InstructionLines:
    """Where each of the ``roles``, lacking instructions in ``file``, is given the line pointing
    it at its copy of them, as :func:`_placement` places it."""
    placements = {name: _placement(file, _role_table(name)) for name in roles}
    return InstructionLines(
        added=_placed(placements, _Placement.ADDED),
        commented=_placed(placements, _Placement.COMMENTED),
        appended=_placed(placements, _Placement.APPENDED),
        inline=_placed(placements, _Placement.INLINE),
    )


def _placement(file: _File, table: str) -> _Placement:
    """Where a role whose table is ``table`` is given its line: appended with the table if
    ``file`` lacks it; nowhere if the table is set without a set header of its own; otherwise in
    its :meth:`_File.role_region`, under its header, set or commented out."""
    if not file.has(table):
        return _Placement.APPENDED
    region = file.role_region(table)
    if region is None:
        return _Placement.INLINE
    header = file.header_of(region)
    return _Placement.COMMENTED if _commented_out(header) else _Placement.ADDED


def _placed(placements: dict[str, _Placement], placement: _Placement) -> tuple[str, ...]:
    """The roles ``placements`` gives ``placement``, in order."""
    return tuple(name for name, placed in placements.items() if placed is placement)


def _commented_out(line: _Line) -> bool:
    """Whether ``line`` is commented out."""
    return line.text.lstrip().startswith("#")


def _instruction_insertions(file: _File, roles: Sequence[str]) -> dict[str, _Insertion]:
    """The line pointing each of the ``roles`` at its copy of its instructions, after the end of
    its table's :meth:`_File.role_region`, by role; none for a role whose table has none."""
    return {name: found for name in roles if (found := _instruction_insertion(file, name))}


def _instruction_insertion(file: _File, name: str) -> _Insertion | None:
    """The line pointing role ``name`` at its copy of its instructions, after the end of its
    table's :meth:`_File.role_region`; none if it has none."""
    table = _role_table(name)
    region = file.role_region(table)
    if region is None:
        return None
    return _Insertion(region.end, f"[{table}]", ("instructions",), (_instructions_line(name),))


def _role_sections(reference: Sequence[_Line], roles: Sequence[str]) -> list[list[str]]:
    """Each of the ``roles``' tables as the ``reference`` has it, which has one for each, with
    the line pointing it at its copy of its instructions after its last line."""
    regions = {region.table: region for region in _regions(reference)}
    return [
        [*_section(reference, regions[_role_table(name)]), _instructions_line(name)]
        for name in roles
    ]


def _instructions_line(name: str) -> str:
    """The line pointing role ``name`` at its copy of its instructions, commented out."""
    return f"# {instructions_setting(name)}"


def _rewritten(document: TOMLDocument, checks: ChecksProposal) -> str:
    """``document`` as TOML with ``checks`` in place of its steps and setup, however it sets
    them."""
    document["verification"] = _verification(checks)
    if checks.setup is None:
        document.pop("setup", None)
    else:
        document["setup"] = _setup(checks.setup)
    return tomlkit.dumps(document)


def _verification(checks: ChecksProposal) -> Table:
    """The table of ``checks``' steps."""
    verification = tomlkit.table(is_super_table=True)
    verification["steps"] = _steps(checks)
    return verification


def _steps(checks: ChecksProposal) -> AoT:
    """The tables of ``checks``' steps, each of its own."""
    steps = tomlkit.aot()
    for step in checks.steps:
        table: Table = tomlkit.table()
        table["name"] = step.name
        table["command"] = list(step.command)
        steps.append(table)
    steps[-1].add(tomlkit.nl())  # A blank line before whatever follows.
    return steps


def _setup(setup: Setup) -> Table:
    """The table of ``setup``."""
    table = tomlkit.table()
    table["command"] = list(setup.command)
    if setup.watch:
        table["watch"] = list(setup.watch)
    return table


def _check_tables(checks: ChecksProposal) -> dict[str, tuple[str, ...]]:
    """The lines of ``checks``' steps' tables and of its setup's, none without one, by header."""
    setup = () if checks.setup is None else _toml_lines("setup", _setup(checks.setup))
    return {_STEPS: _toml_lines("verification", _verification(checks)), _SETUP: setup}


def _toml_lines(key: str, table: Table) -> tuple[str, ...]:
    """The lines of ``table``, under ``key``, as TOML."""
    document = tomlkit.document()
    document[key] = table
    return tuple(tomlkit.dumps(document).strip("\n").split("\n"))


def _same_settings(text: str, other: str) -> bool:
    """Whether ``text`` is valid TOML setting what ``other`` does."""
    try:
        return tomlkit.parse(text).unwrap() == tomlkit.parse(other).unwrap()
    except TOMLKitError:
        return False


def _edited(lines: Sequence[_Line], checks: ChecksProposal) -> str:
    """The text of ``lines`` with ``checks`` in place of the steps' and setup's tables they set
    under headers of their own, and appended if they set none, as :func:`with_checks` has it."""
    tables = _set_tables(lines)
    new = _check_tables(checks)
    runs = {header: _runs(tables, header) for header in new}
    spans = chain.from_iterable(_replacing(runs[header], new[header]) for header in new)
    text = _spliced(lines, spans)
    for header, block in new.items():
        if block and not runs[header]:
            text = [*text, *_apart(text), *block]
    return "\n".join(text) + "\n"


def _set_tables(lines: Sequence[_Line]) -> list[_SetTable]:
    """The tables ``lines`` set under headers of their own, in order."""
    starts = [
        index
        for index, line in enumerate(lines)
        if line.kind is _Kind.HEADER and not _commented_out(line)
    ]
    return [
        _SetTable(lines[start].name, start, _last_setting(lines, start, stop))
        for start, stop in pairwise([*starts, len(lines)])
    ]


def _last_setting(lines: Sequence[_Line], start: int, stop: int) -> int:
    """After the last setting of ``lines`` between the header at ``start`` and ``stop``; after
    the header if there's none."""
    settings = [index for index in range(start + 1, stop) if lines[index].kind is _Kind.SETTING]
    return settings[-1] + 1 if settings else start + 1


def _runs(tables: Sequence[_SetTable], header: str) -> list[_Span]:
    """Each run of ``tables`` under ``header`` with no other table between them, from the first
    one's start to the last one's end."""
    runs = [
        list(run)
        for matches, run in groupby(tables, lambda table: table.header == header)
        if matches
    ]
    return [_Span(run[0].start, run[-1].end) for run in runs]


def _replacing(runs: Sequence[_Span], lines: tuple[str, ...]) -> list[_Span]:
    """``runs``, the first giving way to ``lines`` and the others to none."""
    return [replace(run, lines=lines) for run in runs[:1]] + list(runs[1:])


def _example(lines: Sequence[_Line], region: _Region, tables: tuple[str, ...]) -> _Span:
    """``region`` of ``lines``, an example, from the comments describing it to its last key,
    giving way to ``tables`` under those comments, without the :data:`FOR_EXAMPLE` they end
    with."""
    header = next(
        index for index in range(region.start, region.end) if lines[index].kind is _Kind.HEADER
    )
    described = (line.text.removesuffix(f" {FOR_EXAMPLE}") for line in lines[region.start : header])
    return _Span(region.start, region.end, (*described, *tables))


def _spliced(lines: Sequence[_Line], spans: Iterable[_Span]) -> list[str]:
    """The text of ``lines`` with each of ``spans``' lines in place of those it runs over; one
    giving way to none takes the blank lines just above it with it."""
    text: list[str] = []
    position = 0
    for span in sorted(spans):
        text += [line.text for line in lines[position : span.start]]
        if not span.lines:
            text = _unpadded(text)
        text += span.lines
        position = span.end
    return text + [line.text for line in lines[position:]]


def _unpadded(text: list[str]) -> list[str]:
    """``text`` without the blank lines it ends with."""
    end = len(text)
    while end and not text[end - 1].strip():
        end -= 1
    return text[:end]
