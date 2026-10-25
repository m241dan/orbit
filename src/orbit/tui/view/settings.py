"""The session's settings and the pickers that change them."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, get_args

from orbit.protocol import PLANNER, AgentEntry, Effort, Settings, ToolAccess
from orbit.tui.view.common import model_label

MODEL_TITLE = "Model"
TOOL_ACCESS_TITLE = "Tool access"
EFFORT_TITLE = "Effort"
ROLE_TITLE = "Role"
EFFORT_OPTIONS: tuple[Effort | None, ...] = (None, *get_args(Effort))
"""The efforts a picker offers, none (unset) first."""
UNSET_EFFORT = "unset"
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


def effort_choices(settings: Settings) -> tuple[list[Settings], int | None]:
    """The settings each option of the effort picker gives, ``settings`` with each of
    ``EFFORT_OPTIONS``, and the index of ``settings`` among them."""
    options = [settings.model_copy(update={"effort": e}) for e in EFFORT_OPTIONS]
    return _with_current(options, settings)


def picked_model(choices: Sequence[str], settings: Settings, index: int) -> Settings:
    """``settings`` with the model of option ``index`` of the model picker offering
    ``choices``."""
    return model_choices(choices, settings)[0][index]


def picked_tool_access(settings: Settings, index: int) -> Settings:
    """``settings`` with the tool access of option ``index`` of the tool access picker."""
    return tool_access_choices(settings)[0][index]


def picked_effort(settings: Settings, index: int) -> Settings:
    """``settings`` with the effort of option ``index`` of the effort picker."""
    return effort_choices(settings)[0][index]


def _with_current(options: list[Settings], settings: Settings) -> tuple[list[Settings], int | None]:
    """``options`` and the index of ``settings`` among them, or None when it isn't one."""
    return options, options.index(settings) if settings in options else None


def tool_access_labels() -> list[str]:
    """The labels a picker shows for ``TOOL_ACCESS_OPTIONS``, in the same order."""
    return [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]


def tool_access_label(access: ToolAccess) -> str:
    """How ``access`` is shown beside a model: ``full (auto-approved)``."""
    return TOOL_ACCESS_LABELS[access]


def effort_label(effort: Effort | None) -> str:
    """How ``effort`` shows: its level, or ``unset`` for none."""
    return UNSET_EFFORT if effort is None else effort


def effort_labels() -> list[str]:
    """The labels a picker shows for ``EFFORT_OPTIONS``, in the same order."""
    return [effort_label(e) for e in EFFORT_OPTIONS]


def role_label(role: AgentEntry) -> str:
    """How ``role`` shows among the roles a task can take: ``Coder · work``, its label and its
    lane, then ``personal`` for a personal role."""
    label = f"{role.label} · {role.lane}"
    return f"{label} · personal" if role.personal else label


def settings_fields(settings: Settings) -> tuple[str, str]:
    """How ``settings`` show their model and their tool access, as a task and its editor show
    them: ``("default", "restricted")``."""
    return model_label(settings), tool_access_label(settings.tool_access)


def settings_summary(settings: Settings, role: str = PLANNER) -> str:
    """The settings of the conversational ``role`` as the prompt shows them, its effort only
    when it has one.

    The model and effort are the role's, and the defaults for new tasks; the tool access is
    only the default for new tasks.
    """
    model, tools = settings_fields(settings)
    effort = () if settings.effort is None else (f"effort: {settings.effort}",)
    return " · ".join((f"{role}'s model: {model}", *effort, f"new tasks' tools: {tools}"))


Picked = Callable[[Settings], Awaitable[None] | None]
"""Takes the settings picked."""
Chosen = Callable[[int], Awaitable[None] | None]
"""Takes the index of the option picked."""
PickedRole = Callable[[AgentEntry], None]
"""Takes the role picked."""


class PickersView(Protocol):
    """Where :class:`Pickers` show: a model picker, a tool access picker, an effort picker and a
    role picker."""

    def label_models(self, labels: Sequence[str]) -> None:
        """Label the model picker's options ``labels``."""

    def pick_model(self, current: int | None, chosen: Chosen) -> None:
        """Open the model picker on option ``current``, calling ``chosen`` with the option
        picked; closed, it calls nothing."""

    def pick_tool_access(self, current: int | None, chosen: Chosen) -> None:
        """Open the tool access picker on option ``current``, calling ``chosen`` with the
        option picked; closed, it calls nothing."""

    def pick_effort(self, current: int | None, chosen: Chosen) -> None:
        """Open the effort picker on option ``current``, calling ``chosen`` with the option
        picked; closed, it calls nothing."""

    def pick_role(self, labels: Sequence[str], current: int | None, chosen: Chosen) -> None:
        """Open the role picker offering ``labels`` on option ``current``, calling ``chosen``
        with the option picked; closed, it calls nothing."""


class Pickers:
    """The model, tool access, effort and role pickers, shown in ``view``, and the model choices
    the model picker offers, its labels following them.

    The settings may change while a picker is open, so a pick changes only its own setting of
    the settings as they are when picked.
    """

    def __init__(self, view: PickersView) -> None:
        self._view = view
        self.model_choices: tuple[str, ...] = ()

    def show_choices(self, choices: tuple[str, ...]) -> None:
        """Offer the models ``choices``, relabelling the model picker if they changed."""
        if choices != self.model_choices:
            self._view.label_models(model_options(choices)[1])
        self.model_choices = choices

    def choose_model(self, settings: Callable[[], Settings], then: Picked) -> None:
        """Open the model picker on the model of ``settings``, the settings now, and call
        ``then`` with them, as they are when picked, with the model picked."""
        _, current = model_choices(self.model_choices, settings())
        self._view.pick_model(
            current, lambda index: then(picked_model(self.model_choices, settings(), index))
        )

    def choose_tools(self, settings: Callable[[], Settings], then: Picked) -> None:
        """Open the tool access picker on the tool access of ``settings``, the settings now, and
        call ``then`` with them, as they are when picked, with the tool access picked."""
        _, current = tool_access_choices(settings())
        self._view.pick_tool_access(
            current, lambda index: then(picked_tool_access(settings(), index))
        )

    def choose_effort(self, settings: Callable[[], Settings], then: Picked) -> None:
        """Open the effort picker on the effort of ``settings``, the settings now, and call
        ``then`` with them, as they are when picked, with the effort picked."""
        _, current = effort_choices(settings())
        self._view.pick_effort(current, lambda index: then(picked_effort(settings(), index)))

    def choose_role(self, roles: Sequence[AgentEntry], role: str, then: PickedRole) -> None:
        """Open the role picker offering ``roles`` on the one named ``role``, if it's among
        them, and call ``then`` with the role picked."""
        names = [entry.name for entry in roles]
        current = names.index(role) if role in names else None
        labels = [role_label(entry) for entry in roles]
        self._view.pick_role(labels, current, lambda index: then(roles[index]))
