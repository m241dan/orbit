"""The session's settings and the pickers that change them."""

from collections.abc import Sequence
from typing import get_args

from orbit.protocol import Settings, ToolAccess
from orbit.tui.view.common import model_label

MODEL_TITLE = "Model"
TOOL_ACCESS_TITLE = "Tool access"
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


def model_choices(choices: Sequence[str], settings: Settings) -> tuple[list[Settings], int | None]:
    """The settings each option of the model picker gives, ``settings`` with the default or one
    of ``choices`` as its model, and the index of ``settings`` among them, if it's there."""
    models, _ = model_options(choices)
    return _with_current([settings.model_copy(update={"model": m}) for m in models], settings)


def tool_access_choices(settings: Settings) -> tuple[list[Settings], int | None]:
    """The settings each option of the tool access picker gives, ``settings`` with each of
    ``TOOL_ACCESS_OPTIONS``, and the index of ``settings`` among them."""
    options = [settings.model_copy(update={"tool_access": a}) for a in TOOL_ACCESS_OPTIONS]
    return _with_current(options, settings)


def picked_model(choices: Sequence[str], settings: Settings, index: int) -> Settings:
    """``settings`` with the model of option ``index`` of the model picker offering
    ``choices``."""
    return model_choices(choices, settings)[0][index]


def picked_tool_access(settings: Settings, index: int) -> Settings:
    """``settings`` with the tool access of option ``index`` of the tool access picker."""
    return tool_access_choices(settings)[0][index]


def _with_current(options: list[Settings], settings: Settings) -> tuple[list[Settings], int | None]:
    """``options`` and the index of ``settings`` among them, or None when it isn't one."""
    return options, options.index(settings) if settings in options else None


def tool_access_labels() -> list[str]:
    """The labels a picker shows for ``TOOL_ACCESS_OPTIONS``, in the same order."""
    return [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]


def tool_access_label(access: ToolAccess) -> str:
    """How ``access`` is shown beside a model: ``full (auto-approved)``."""
    return TOOL_ACCESS_LABELS[access]


def settings_fields(settings: Settings) -> tuple[str, str]:
    """How ``settings`` show their model and their tool access, as a task and its editor show
    them: ``("default", "restricted")``."""
    return model_label(settings), tool_access_label(settings.tool_access)


def settings_summary(settings: Settings) -> str:
    """The settings as the prompt shows them.

    The model is the planner's, and the default for new tasks; the tool access is
    only the default for new tasks.
    """
    model, tools = settings_fields(settings)
    return f"planner's model: {model} · new tasks' tools: {tools}"
