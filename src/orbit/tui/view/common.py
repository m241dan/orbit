"""What the other view modules share: entry kinds, the spinner, status icons and model labels."""

from typing import Literal

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
