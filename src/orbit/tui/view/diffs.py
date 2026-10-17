"""The changes a call to an editing tool makes to a file, as the lines of a diff."""

import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

EDIT_SEPARATOR = "⋯"
"""The line between the diffs of a MultiEdit's edits."""

type Differ = Callable[[Mapping[str, Any]], list[str] | None]


@dataclass(frozen=True)
class FileDiff:
    """The ``lines`` of the diff an edit makes to the file at ``path``, each starting with
    ``-`` if removed, ``+`` if added or a space if unchanged."""

    path: str
    lines: tuple[str, ...]


def file_diff(name: str, arguments: Mapping[str, Any]) -> FileDiff | None:
    """The diff that a call to the tool ``name`` with ``arguments`` makes; None unless it's
    an Edit, MultiEdit or Write with the arguments it needs."""
    differ = DIFFERS.get(name)
    path = arguments.get("file_path")
    if differ is None or not isinstance(path, str):
        return None
    lines = differ(arguments)
    return None if lines is None else FileDiff(path, tuple(lines))


def line_diff(old: str, new: str) -> list[str]:
    """The lines of ``old`` and ``new`` marked as removed, added or unchanged, in order."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    lines: list[str] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            lines += [f" {line}" for line in old_lines[old_start:old_end]]
            continue
        lines += [f"-{line}" for line in old_lines[old_start:old_end]]
        lines += [f"+{line}" for line in new_lines[new_start:new_end]]
    return lines


def _edit(edit: object) -> list[str] | None:
    """The diff of ``edit``, or None unless it has both strings."""
    if not isinstance(edit, Mapping):
        return None
    old, new = edit.get("old_string"), edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return line_diff(old, new)


def _multi_edit(arguments: Mapping[str, Any]) -> list[str] | None:
    """The diffs of each of the edits, a separator line between them."""
    edits = arguments.get("edits")
    if not isinstance(edits, list):
        return None
    lines: list[str] = []
    for index, edit in enumerate(edits):
        diff = _edit(edit)
        if diff is None:
            return None
        lines += [EDIT_SEPARATOR, *diff] if index else diff
    return lines


def _write(arguments: Mapping[str, Any]) -> list[str] | None:
    """Every line of the content written, as added."""
    content = arguments.get("content")
    return None if not isinstance(content, str) else [f"+{line}" for line in content.splitlines()]


DIFFERS: Mapping[str, Differ] = MappingProxyType(
    {"Edit": _edit, "MultiEdit": _multi_edit, "Write": _write}
)
"""How to find the diff of a call to each of the tools that edit files."""
