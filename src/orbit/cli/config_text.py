"""How ``orbit config show`` prints the configuration it reports: as text, a line per setting,
or as one JSON object; and how ``--skills`` prints the skill map: as a table, or as one JSON
object."""

import json
from itertools import groupby
from pathlib import Path
from typing import Any

from orbit.agent import (
    LIMIT_KEYS,
    MAPPED_CELL,
    NEVER_CELL,
    REVIEWED_TASK_CELL,
    ROLE_CELL,
    TASK_CELL,
    ConfigReport,
    Instructions,
    Layer,
    Setting,
    SkillFileText,
    SkillMap,
    key_parts,
)
from orbit.tui.view.common import counted

type Text = Instructions | SkillFileText
"""A value shown by its length, or with ``full`` as its text."""

GROUPED = frozenset({"roles", "triggers", "mcp", "skill_files"})
"""The sections listed in a group for each of their tables: each role, each trigger, each MCP
server, and each skill."""

COLUMN_GAP = "   "
"""What parts the skill map's columns."""

SKILL_MAP_LEGEND = (
    f"{ROLE_CELL}: in the role's skills.",
    f"{TASK_CELL}: when the task's list names it.",
    f"{REVIEWED_TASK_CELL}: when the reviewed task's list names it.",
    f"{MAPPED_CELL}: in [[skills]]; always given, after the task's list.",
    f"{NEVER_CELL}: never given.",
)
"""The lines under the skill map, saying what each value of its cells means."""


def config_text(report: ConfigReport, *, full: bool, root: Path, home: Path) -> str:
    """``report`` as text: a line per setting, ``<key> = <value>   <layer>``, the limits, each
    role, each trigger, each MCP server, each skill's files and each other section in a group of
    their own, apart
    from the next by a blank line; each value as JSON but roles' instructions and triggers'
    prompts, which show as the file they're in and their length, and skill files, which show
    their length, or any with ``full`` their text. A layer's file shows relative to the
    workspace at ``root`` inside it, and from ``~`` inside ``home``."""
    groups = groupby(report.items(), key=lambda item: _group(item[0]))
    return "\n\n".join(
        "\n".join(_line(key, setting, full, root, home) for key, setting in settings)
        for _, settings in groups
    )


def config_json(report: ConfigReport, *, full: bool) -> str:
    """``report`` as one JSON object, nested by the parts of each key but a skill file's, nested
    by its skill only with its path whole, each setting an object of its ``value``, as the text
    gives it but for being JSON, its ``layer``'s name and that layer's ``file``, ``null`` if it
    has none."""
    tree: dict[str, Any] = {}
    for key, setting in report.items():
        *parents, name = key_parts(key)
        node = tree
        for part in parents:
            node = node.setdefault(part, {})
        node[name] = _json(setting, full)
    return json.dumps(tree, indent=2, ensure_ascii=False)


def skill_map_text(skills: SkillMap) -> str:
    """``skills`` as a table, a header row, ``skill`` then each role's name, then a row per skill,
    its name then its cells, each column as wide as its widest cell; then, after a blank line,
    the :data:`SKILL_MAP_LEGEND`."""
    rows = [
        ("skill", *skills.roles),
        *((name, *(cells[role] for role in skills.roles)) for name, cells in skills.cells.items()),
    ]
    widths = [max(map(len, column)) for column in zip(*rows, strict=True)]
    lines = (
        COLUMN_GAP.join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )
    return "\n".join(lines) + "\n\n" + "\n".join(SKILL_MAP_LEGEND)


def skill_map_json(skills: SkillMap) -> str:
    """``skills`` as one JSON object, its cells by skill and role under ``skills``."""
    cells = {name: dict(row) for name, row in skills.cells.items()}
    return json.dumps({"skills": cells}, indent=2, ensure_ascii=False)


def _group(key: str) -> str:
    """The group the setting at ``key`` is listed in: the limits', its role's, its trigger's, its
    MCP server's, its skill's, or its section's."""
    if key in LIMIT_KEYS:
        return ""
    section, *rest = key.split(".")
    return f"{section}.{rest[0]}" if section in GROUPED and rest else section


def _line(key: str, setting: Setting, full: bool, root: Path, home: Path) -> str:
    """The line of ``setting``, at ``key``, as :func:`config_text` gives it."""
    return f"{key} = {_value(setting.value, full)}   {_layer(setting.layer, root, home)}"


def _value(value: Any, full: bool) -> str:
    """``value`` as text: as JSON, but instructions and skill files as :func:`_summary` gives
    them, or with ``full`` their text, on lines of their own between triple quotes, a skill
    file's without the line break it ends with."""
    if not isinstance(value, Instructions | SkillFileText):
        return json.dumps(value, ensure_ascii=False)
    if not full:
        return _summary(value)
    text = value.text if isinstance(value, Instructions) else value.text.removesuffix("\n")
    return f'"""\n{text}\n"""'


def _json(setting: Setting, full: bool) -> dict[str, Any]:
    """``setting`` as a JSON object, as :func:`config_json` gives it."""
    value = setting.value
    if isinstance(value, Instructions | SkillFileText):
        value = value.text if full else _summary(value)
    return {"value": value, "layer": setting.layer.name, "file": setting.layer.path}


def _summary(text: Text) -> str:
    """``text`` as how many lines it has, after the file instructions are in, or ``inline``."""
    lines = counted(text.lines, "line")
    if isinstance(text, SkillFileText):
        return f"({lines})"
    return f"({text.file or 'inline'}, {lines})"


def _layer(layer: Layer, root: Path, home: Path) -> str:
    """``layer``'s name, then its file's path, if it has one, as :func:`config_text` shows it
    for the workspace at ``root`` and the ``home`` directory."""
    if layer.path is None:
        return layer.name
    return f"{layer.name} {_shown(Path(layer.path), root, home)}"


def _shown(path: Path, root: Path, home: Path) -> str:
    """``path`` relative to the workspace at ``root`` inside it, from ``~`` inside ``home``, and
    otherwise whole."""
    if path.is_relative_to(root):
        return str(path.relative_to(root))
    if path.is_relative_to(home):
        return f"~/{path.relative_to(home)}"
    return str(path)
