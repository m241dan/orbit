"""How ``orbit config show`` prints the configuration it reports: as text, a line per setting,
or as one JSON object."""

import json
from itertools import groupby
from pathlib import Path
from typing import Any

from orbit.agent import LIMIT_KEYS, ConfigReport, Instructions, Layer, Setting


def config_text(report: ConfigReport, *, full: bool, root: Path, home: Path) -> str:
    """``report`` as text: a line per setting, ``<key> = <value>   <layer>``, the limits, each
    role and each other section in a group of their own, apart from the next by a blank line;
    each value as JSON but roles' instructions, which show as the file they're in and their
    length, or with ``full`` their text. A layer's file shows relative to the workspace at
    ``root`` inside it, and from ``~`` inside ``home``."""
    groups = groupby(report.items(), key=lambda item: _group(item[0]))
    return "\n\n".join(
        "\n".join(_line(key, setting, full, root, home) for key, setting in settings)
        for _, settings in groups
    )


def config_json(report: ConfigReport, *, full: bool) -> str:
    """``report`` as one JSON object, nested by the parts of each key, each setting an object of
    its ``value``, as the text gives it but for being JSON, its ``layer``'s name and that
    layer's ``file``, ``null`` if it has none."""
    tree: dict[str, Any] = {}
    for key, setting in report.items():
        *parents, name = key.split(".")
        node = tree
        for part in parents:
            node = node.setdefault(part, {})
        node[name] = _json(setting, full)
    return json.dumps(tree, indent=2, ensure_ascii=False)


def _group(key: str) -> str:
    """The group the setting at ``key`` is listed in: the limits', its role's, or its
    section's."""
    if key in LIMIT_KEYS:
        return ""
    section, *rest = key.split(".")
    return f"{section}.{rest[0]}" if section == "roles" else section


def _line(key: str, setting: Setting, full: bool, root: Path, home: Path) -> str:
    """The line of ``setting``, at ``key``, as :func:`config_text` gives it."""
    return f"{key} = {_value(setting.value, full)}   {_layer(setting.layer, root, home)}"


def _value(value: Any, full: bool) -> str:
    """``value`` as text: as JSON, but instructions as :func:`_summary` gives them, or with
    ``full`` their text, on lines of their own between triple quotes."""
    if not isinstance(value, Instructions):
        return json.dumps(value, ensure_ascii=False)
    return f'"""\n{value.text}\n"""' if full else _summary(value)


def _json(setting: Setting, full: bool) -> dict[str, Any]:
    """``setting`` as a JSON object, as :func:`config_json` gives it."""
    value = setting.value
    if isinstance(value, Instructions):
        value = value.text if full else _summary(value)
    return {"value": value, "layer": setting.layer.name, "file": setting.layer.path}


def _summary(instructions: Instructions) -> str:
    """``instructions`` as the file they're in, or ``inline``, and how many lines they have."""
    lines = instructions.lines
    return f"({instructions.file or 'inline'}, {lines} line{'' if lines == 1 else 's'})"


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
