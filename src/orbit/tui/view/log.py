"""A tab's log of transcript entries: which of them are shown, and where."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from orbit.tui.view.common import EntryKind
from orbit.tui.view.tool_calls import ToolCall
from orbit.tui.view.transcript import ruled


@dataclass(frozen=True)
class Entry:
    """A transcript entry: its ``text``, the ``kind`` it's styled as, the ``agent`` it's by,
    if any, and the ``speaker`` it's spoken by (see :func:`speaker`). An agent's message thinking
    out loud keeps the ``turn`` it's from, and is drawn dim, until it's found to be the turn's
    final answer. A tool call's entry keeps the ``call``, which its text shows in a line."""

    text: str
    kind: EntryKind
    agent: str | None = None
    turn: str | None = None
    call: ToolCall | None = None
    speaker: str | None = None


def shown(entry: Entry, thinking_hidden: bool) -> bool:
    """Whether ``entry`` is shown: all are, but an agent's thinking out loud when
    ``thinking_hidden``."""
    return not (thinking_hidden and entry.turn is not None)


def shown_rows(entries: Sequence[Entry], thinking_hidden: bool) -> list[int]:
    """Where the ``entries`` shown are among them, in order: the entry each option shows."""
    return [i for i, entry in enumerate(entries) if shown(entry, thinking_hidden)]


def nearest_row(rows: Sequence[int], index: int) -> int | None:
    """The option among ``rows`` of the entry at ``index``, or of the nearest shown entry
    before it, else after it; None when none is shown."""
    before = [row for row, entry in enumerate(rows) if entry <= index]
    if before:
        return before[-1]
    return 0 if rows else None


def after_rows(rows: Sequence[int], indexes: Sequence[int]) -> bool:
    """Whether every entry at ``indexes`` comes after the last entry ``rows`` shows."""
    return not rows or all(index > rows[-1] for index in indexes)


def thinking_places(entries: Sequence[Entry], agent: str | None, turn: str) -> list[int]:
    """Where among ``entries`` the messages ``agent`` is thinking out loud in ``turn`` are, in
    order."""
    return [i for i, entry in enumerate(entries) if (entry.agent, entry.turn) == (agent, turn)]


def latest_rule(entries: Sequence[Entry], agent: str) -> int | None:
    """Where among ``entries`` ``agent``'s latest rule is; None when it has none."""
    rules = [i for i, entry in enumerate(entries) if entry.agent == agent and ruled(entry.kind)]
    return rules[-1] if rules else None


def entry_action(entry: Entry) -> Literal["inspect", "toggle"]:
    """What choosing ``entry`` does: a tool call or request is inspected, anything else is
    collapsed or expanded."""
    return "toggle" if entry.call is None else "inspect"


def entry_component(entry: Entry) -> str:
    """The component class ``entry`` is styled by: an agent's thinking out loud is styled as
    thinking, anything else as its kind."""
    component = "thinking" if entry.turn is not None else entry.kind
    return f"transcript--{component}"
