"""What the other view modules share: entry kinds, the spinner, status icons, model labels
and token counts."""

from typing import Literal

from rich.cells import cell_len, set_cell_size

from orbit.protocol import Settings, StepStatus, TaskStatus

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
