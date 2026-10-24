"""The task editor: the fields it shows, which is selected, and the task it saves."""

from collections.abc import Callable
from typing import Protocol

from orbit.protocol import Settings, TaskSpec
from orbit.tui.view.settings import settings_fields
from orbit.tui.view.tasks import join_prompt, split_prompt, task_editor_title

FIELDS = ("task-title", "task-prompt", "task-model", "task-tools")
"""The editor's fields, in the order j/k move through them."""
TEXT_FIELDS = FIELDS[:2]


class SettingsChooser(Protocol):
    """The pickers that change a model and tool access: each opens on the settings it's given
    and passes on those settings, as they are when picked, with what's picked."""

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

    def show_settings(self, model: str, tools: str) -> None:
        """Show ``model`` and ``tools`` as the task's model and tool access."""
        ...

    def dismiss(self, result: TaskSpec | None) -> object:
        """Close the editor with ``result``."""
        ...


class TaskEditor:
    """The editor of one task, ``task``, or of a new task taking the session's ``settings``,
    shown in ``view``, changing its model and tool access with ``pickers``: it closes with the
    task saved, or none.

    The title is the prompt's first line and the prompt box its body, joined on save by
    ``join_prompt``; a task without a title isn't saved. j/k select a field, i types in the
    title or prompt, typing in the title when another field is selected, and enter on the
    model or tools opens its picker.
    """

    def __init__(
        self,
        view: TaskEditorView,
        task: TaskSpec | None,
        settings: Settings,
        pickers: SettingsChooser,
    ) -> None:
        self._view = view
        self._pickers = pickers
        self.heading = task_editor_title(new=task is None)
        self._prompt = "" if task is None else task.prompt
        self.settings = settings if task is None else task.settings
        self._role = TaskSpec.model_fields["role"].default if task is None else task.role
        self._field = 0

    @property
    def text(self) -> tuple[str, str]:
        """The title and the prompt's body the editor starts with."""
        return split_prompt(self._prompt)

    @property
    def settings_shown(self) -> tuple[str, str]:
        """How the task's model and tool access show."""
        return settings_fields(self.settings)

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
        """Open the picker of the field selected, if it's the model or the tool access."""
        if self.selected == "task-model":
            self._pickers.choose_model(lambda: self.settings, self._update)
        elif self.selected == "task-tools":
            self._pickers.choose_tools(lambda: self.settings, self._update)

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

    def _update(self, settings: Settings) -> None:
        """Take ``settings``, as picked, and show them."""
        self.settings = settings
        self._view.show_settings(*settings_fields(settings))
