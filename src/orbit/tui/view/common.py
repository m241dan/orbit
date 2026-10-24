"""What the other view modules share: entry kinds, the spinner, status icons, model labels,
the wording of a wait for a coder, token counts, counted nouns, the lines of text, where a
list's highlight goes, the ids of a list's options, the keys a picker shows and the active
session."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rich.cells import cell_len, set_cell_size

from orbit.protocol import SessionSummary, Settings, StepStatus, TaskStatus

EntryKind = Literal[
    "user",
    "prompt",
    "assistant",
    "tool",
    "verify",
    "agent",
    "request",
    "error",
    "rule",
    "error_rule",
]
"""How a transcript entry is styled."""


# The arc spinner beside work in progress, and how fast it turns.
SPINNER_FRAMES = "◜◠◝◞◡◟"
SPINNER_FPS = 8

# Emoji drawn two cells wide on their own (no variation selector), so rows line up.
# Running tasks and steps show the spinner instead.
STATUS_ICONS: dict[TaskStatus, str] = {
    "pending": "⏳",
    "verifying": "🔍",
    "completed": "✅",
    "unverified": "❗",
    "cancelled": "🛑",
    "error": "❌",
    "gave_up": "🏳",
    "draft": "📝",
}
STEP_ICONS: dict[StepStatus, str] = {
    "pending": "⏳",
    "passed": "✅",
    "failed": "❌",
    "cancelled": "🛑",
    "skipped": "⏩",
}


def spinner_frame(now: float) -> str:
    """The spinner frame at ``now`` seconds, padded to two columns like the emoji beside it."""
    return SPINNER_FRAMES[int(now * SPINNER_FPS) % len(SPINNER_FRAMES)] + " "


def text_lines(text: str) -> list[str]:
    """``text``'s lines, split only at newlines, so a form feed or the like stays in its line;
    none for empty text, and without a carriage return before a newline or an empty line
    after the last newline."""
    if not text:
        return []
    return [line.removesuffix("\r") for line in text.removesuffix("\n").split("\n")]


def counted(count: int, singular: str, plural: str | None = None) -> str:
    """``count`` and ``singular``, or ``plural`` (``singular`` and an s by default) unless the
    count is one: ``1 line``, ``2 lines``."""
    noun = singular if count == 1 else plural or f"{singular}s"
    return f"{count} {noun}"


def ordinal(number: int) -> str:
    """``number`` as an ordinal: ``1st``, ``2nd``, ``3rd``, ``4th``, ``11th``, ``21st``."""
    suffixes = {1: "st", 2: "nd", 3: "rd"}
    teen = number % 100 in (11, 12, 13)
    suffix = "th" if teen else suffixes.get(number % 10, "th")
    return f"{number}{suffix}"


CODER_WAIT = "waiting for a coder"
"""A wait for a coder, in words, where nothing more is known about it."""


def coder_place(position: int | None) -> str | None:
    """Where a run at ``position`` in line for a coder is: ``next`` at 1, else the ordinal
    (``2nd``); none without a position."""
    if position is None:
        return None
    return "next" if position == 1 else ordinal(position)


def coder_wait_note(detail: str) -> str:
    """A short note that a run waits for a coder, with ``detail``: ``waiting: 2nd``."""
    return f"waiting: {detail}"


def model_label(settings: Settings) -> str:
    return settings.model or "default"


def clipped(text: str, limit: int) -> str:
    """``text``, or as much of it as fits ``limit`` cells with an ellipsis after it."""
    return text if cell_len(text) <= limit else f"{set_cell_size(text, limit - 1)}…"


TOKEN_UNITS = ((1_000_000, "M"), (1_000, "k"))
"""The units token counts are shown in, largest first."""


def token_text(count: int) -> str:
    """``count`` tokens, compact: as they are below a thousand, else in thousands (``k``) or
    millions (``M``), rounded down, with one decimal below 10 of the unit (``1.2M``, ``14k``)."""
    for unit, suffix in TOKEN_UNITS:
        if count >= unit:
            return _in_unit(count, unit) + suffix
    return str(count)


def _in_unit(count: int, unit: int) -> str:
    tenths = count * 10 // unit
    if tenths >= 100 or tenths % 10 == 0:
        return str(count // unit)
    return f"{tenths // 10}.{tenths % 10}"


def kept_highlight(
    shown_keys: Sequence[str | None], highlighted: int | None, keys: Sequence[str | None]
) -> int | None:
    """Where the highlight on row ``highlighted`` of rows with ``shown_keys`` goes once rows
    with ``keys`` are shown, a trailing action row's key being None: on the row with its key,
    or, when that's gone, on the row where it was, kept within the keyed rows, never moving
    onto the trailing action row; nowhere when nothing was highlighted, the trailing row it
    was on has gone or no keyed rows are left."""
    if highlighted is None or not keys:
        return None
    key = shown_keys[highlighted]
    if key in keys:
        return list(keys).index(key)
    return None if key is None else _nearest_keyed(highlighted, keys)


def _nearest_keyed(index: int, keys: Sequence[str | None]) -> int | None:
    """The keyed row of rows with ``keys`` nearest row ``index``, or None when there's none."""
    keyed = sum(1 for key in keys if key is not None)
    return min(index, keyed - 1) if keyed else None


ACTION_OPTION = "action"
"""The id of a list's trailing action row's option."""


def option_id(key: str | None) -> str:
    """The id of the option for the row with ``key``, a trailing action row's key being None."""
    return ACTION_OPTION if key is None else f"row {key}"


def picked_row(picked: str | None, keys: Sequence[str | None]) -> int | None:
    """Which of the rows with ``keys`` has the option with the id ``picked``; none once that
    row has gone."""
    ids = [option_id(key) for key in keys]
    return ids.index(picked) if picked in ids else None


@dataclass(frozen=True)
class ListHints:
    """The keys a picker's list shows: ``closing`` where escape closes it, and ``quitting`` at
    startup, where escape quits orbit."""

    closing: str
    quitting: str


def picker_hint(hints: ListHints, *, startup: bool, box: str | None) -> str:
    """The keys a picker shows: its text box's, ``box``, while that's shown, or else its list's
    ``hints``, where escape quits at ``startup``."""
    if box is not None:
        return box
    return hints.quitting if startup else hints.closing


def active_session(sessions: Sequence[SessionSummary]) -> SessionSummary | None:
    """The active session among ``sessions``; none if none is."""
    return next((summary for summary in sessions if summary.active), None)
