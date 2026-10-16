"""The session's settings and the pickers that change them."""

from collections.abc import Sequence
from typing import get_args

from orbit.protocol import Settings, ToolAccess
from orbit.tui.view.common import model_label

TOOL_ACCESS_OPTIONS: tuple[ToolAccess, ...] = get_args(ToolAccess)
TOOL_ACCESS_LABELS: dict[ToolAccess, str] = {
    "off": "off",
    "restricted": "restricted",
    "full": "full (auto-approved)",
}
TOOL_ACCESS_DESCRIPTIONS: dict[ToolAccess, str] = {
    "off": "off: no tools",
    "restricted": "restricted: read-only; anything needing approval is refused",
    "full": "full: auto-approve everything",
}


def model_options(choices: Sequence[str]) -> tuple[tuple[str | None, ...], list[str]]:
    """The models a picker offers, the default (None) first, and their labels."""
    return (None, *choices), ["default", *choices]


def tool_access_labels() -> list[str]:
    """The labels a picker shows for ``TOOL_ACCESS_OPTIONS``, in the same order."""
    return [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]


def settings_summary(settings: Settings) -> str:
    """The settings as the prompt shows them.

    The model is the planner's, and the default for new tasks; the tool access is
    only the default for new tasks.
    """
    tools = TOOL_ACCESS_LABELS[settings.tool_access]
    return f"planner's model: {model_label(settings)} · new tasks' tools: {tools}"
