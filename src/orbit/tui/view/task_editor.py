"""The task editor: the fields it shows, which is selected, and the task it saves."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from orbit.protocol import AgentEntry, Settings, TaskSpec
from orbit.tui.view.settings import effort_label, role_label, settings_fields
from orbit.tui.view.tasks import join_prompt, split_prompt, task_editor_title

FIELDS = ("task-title", "task-prompt", "task-role", "task-model", "task-tools", "task-effort")
"""The editor's fields, in the order j/k move through them."""
TEXT_FIELDS = FIELDS[:2]


@dataclass(frozen=True)
class TaskChoices:
    """What the editor offers a task: the session's ``settings``, whose model and tool access a
    new task starts with, and the ``agents`` the catalog lists, whose task roles it can take."""

    settings: Settings
    agents: tuple[AgentEntry, ...] = ()


def task_roles(agents: Iterable[AgentEntry]) -> tuple[AgentEntry, ...]:
    """The roles among ``agents`` a task can take: those that aren't conversational."""
    return tuple(agent for agent in agents if not agent.conversational)


def new_task_settings(settings: Settings) -> Settings:
    """The settings a new task starts with: ``settings``' model and tool access, with effort
    unset, as the session's effort is its conversational role's own."""
    return settings.model_copy(update={"effort": None})


def role_defaults(settings: Settings, role: AgentEntry, picked: Iterable[str]) -> Settings:
    """``settings`` with ``role``'s model, tool access and effort, but in the fields
    ``picked``, which keep their own."""
    defaults = {"model": role.model, "tool_access": role.tool_access, "effort": role.effort}
    kept = frozenset(picked)
    return settings.model_copy(update={k: v for k, v in defaults.items() if k not in kept})


class SettingsChooser(Protocol):
    """The pickers that change a model, tool access and effort, each opening on the settings
    it's given and passing on those settings, as they are when picked, with what's picked; and
    the role picker."""

    def choose_model(
        self, settings: Callable[[], Settings], then: Callable[[Settings], None]
    ) -> None:
        """Open the model picker on the model of ``settings``, the settings now, and call
        ``then`` with them, as they are when picked, with the model picked."""
        ...

    def choose_tools(
        self, settings: Callable[[], Settings], then: Callable[[Settings], None]
    ) -> None:
        """Open the tool access picker on the tool access of ``settings``, the settings now,
        and call ``then`` with them, as they are when picked, with the tool access picked."""
        ...

    def choose_effort(
        self, settings: Callable[[], Settings], then: Callable[[Settings], None]
    ) -> None:
        """Open the effort picker on the effort of ``settings``, the settings now, and call
        ``then`` with them, as they are when picked, with the effort picked."""
        ...

    def choose_role(
        self, roles: Sequence[AgentEntry], role: str, then: Callable[[AgentEntry], None]
    ) -> None:
        """Open the role picker offering ``roles`` on the one named ``role``, if it's among
        them, and call ``then`` with the role picked."""
        ...


class TaskEditorView(Protocol):
    """What the task editor shows and what the user has typed in it: the text fields, which
    keep the title and the prompt's body as the user types them."""

    @property
    def typed_title(self) -> str:
        """The text in the title field."""
        ...

    @property
    def typed_body(self) -> str:
        """The text in the prompt box."""
        ...

    def select_field(self, previous: str, field: str) -> None:
        """Mark field ``field`` selected in place of field ``previous``, scrolling it into
        view."""
        ...

    def type_title(self) -> None:
        """Type in the title field."""
        ...

    def type_body(self) -> None:
        """Type in the prompt box."""
        ...

    def show_role(self, role: str) -> None:
        """Show ``role`` as the task's role."""
        ...

    def show_settings(self, model: str, tools: str, effort: str) -> None:
        """Show ``model``, ``tools`` and ``effort`` as the task's model, tool access and
        effort."""
        ...

    def dismiss(self, result: TaskSpec | None) -> object:
        """Close the editor with ``result``."""
        ...


class TaskEditor:
    """The editor of one task, ``task``, or of a new task in the ``coder`` role taking the
    session's model and tool access, with effort unset, shown in ``view``, offering the
    ``choices``' task roles and changing its role, model, tool access and effort with
    ``pickers``: it closes with the task saved, or none.

    The title is the prompt's first line and the prompt box its body, joined on save by
    ``join_prompt``; a task without a title isn't saved. j/k select a field, i types in the
    title or prompt, typing in the title when another field is selected, and enter on the
    role, model, tools or effort opens its picker. Picking a role fills the model, tool access
    and effort with its defaults, but those picked in this edit.
    """

    def __init__(
        self,
        view: TaskEditorView,
        task: TaskSpec | None,
        choices: TaskChoices,
        pickers: SettingsChooser,
    ) -> None:
        self._view = view
        self._pickers = pickers
        self.heading = task_editor_title(new=task is None)
        self._prompt = "" if task is None else task.prompt
        self.settings = new_task_settings(choices.settings) if task is None else task.settings
        self._role = TaskSpec.model_fields["role"].default if task is None else task.role
        self._roles = task_roles(choices.agents)
        self._picked: set[str] = set()  # The settings' fields picked in this edit.
        self._field = 0

    @property
    def text(self) -> tuple[str, str]:
        """The title and the prompt's body the editor starts with."""
        return split_prompt(self._prompt)

    @property
    def role_shown(self) -> str:
        """How the task's role shows: as the roles offered show it, or by its name when it isn't
        one of them."""
        offered = {role.name: role_label(role) for role in self._roles}
        return offered.get(self._role, self._role)

    @property
    def settings_shown(self) -> tuple[str, str, str]:
        """How the task's model, tool access and effort show."""
        return (*settings_fields(self.settings), effort_label(self.settings.effort))

    @property
    def selected(self) -> str:
        """The field selected."""
        return FIELDS[self._field]

    def move(self, step: int) -> None:
        """Select the field ``step`` fields on, stopping at the first and the last."""
        self._select(min(max(self._field + step, 0), len(FIELDS) - 1))

    def insert(self) -> None:
        """Type in the selected text field, or in the title when another is selected."""
        if self.selected not in TEXT_FIELDS:
            self._select(0)
        if self.selected == "task-title":
            self._view.type_title()
        else:
            self._view.type_body()

    def choose(self) -> None:
        """Open the picker of the field selected, if it's the role, the model, the tool access
        or the effort."""
        match self.selected:
            case "task-role":
                self._pickers.choose_role(self._roles, self._role, self._take_role)
            case "task-model":
                self._pickers.choose_model(self._now, partial(self._pick, "model"))
            case "task-tools":
                self._pickers.choose_tools(self._now, partial(self._pick, "tool_access"))
            case "task-effort":
                self._pickers.choose_effort(self._now, partial(self._pick, "effort"))

    def save(self) -> None:
        """Close with the task, if it has a title; its body may be empty."""
        title = self._view.typed_title
        if title.strip():
            prompt = join_prompt(title, self._view.typed_body)
            self._view.dismiss(TaskSpec(prompt=prompt, settings=self.settings, role=self._role))

    def cancel(self) -> None:
        """Close with no task."""
        self._view.dismiss(None)

    def _select(self, field: int) -> None:
        """Select field ``field``."""
        self._view.select_field(self.selected, FIELDS[field])
        self._field = field

    def _now(self) -> Settings:
        """The task's settings now."""
        return self.settings

    def _pick(self, field: str, settings: Settings) -> None:
        """Take ``settings``, with the field ``field`` picked, keeping that field from the roles
        picked after."""
        self._picked.add(field)
        self._update(settings)

    def _take_role(self, role: AgentEntry) -> None:
        """Take ``role``, as picked, and its defaults, but in the fields picked, and show
        them."""
        self._role = role.name
        self._view.show_role(self.role_shown)
        self._update(role_defaults(self.settings, role, self._picked))

    def _update(self, settings: Settings) -> None:
        """Take ``settings`` and show them."""
        self.settings = settings
        self._view.show_settings(*self.settings_shown)
