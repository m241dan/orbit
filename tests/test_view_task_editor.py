"""The task editor without the screen showing it: the fields it starts with, moving between
them, the pickers it opens and what they pick, and the task it saves or discards."""

from collections.abc import Callable

from orbit.protocol import Settings, TaskSpec, TaskState
from orbit.tui.view.settings import (
    model_choices,
    picked_model,
    picked_tool_access,
    tool_access_choices,
)
from orbit.tui.view.task_editor import TaskEditor
from orbit.tui.view.tasks import task_label

FIX_PROMPT = (
    "Fix the failed verification step: x\n\n"
    "The step `x` ran `false` and failed.\n\nFind and fix the cause so the command passes."
)
DEFAULTS = Settings()


class View:
    """What the task editor shows, as it shows it, and what the user has typed in it: the
    title and the prompt's body, set as a test types."""

    def __init__(self) -> None:
        self.typed_title = ""
        self.typed_body = ""
        self.selections: list[tuple[str, str]] = []
        self.typing: list[str] = []
        self.settings: tuple[str, str] | None = None
        self.dismissed: list[TaskSpec | None] = []

    def select_field(self, previous: str, field: str) -> None:
        self.selections.append((previous, field))

    def type_title(self) -> None:
        self.typing.append("title")

    def type_body(self) -> None:
        self.typing.append("body")

    def show_settings(self, model: str, tools: str) -> None:
        self.settings = (model, tools)

    def dismiss(self, result: TaskSpec | None) -> None:
        self.dismissed.append(result)


class Pickers:
    """Pickers offering the models ``choices``, which pick as the app's do: each opens on the
    option of the settings it's given, recorded as ``current``, and a pick passes on those
    settings, as they are when picked, with the option picked."""

    def __init__(self, choices: tuple[str, ...] = ("opus", "sonnet")) -> None:
        self.choices = choices
        self.opened: list[str] = []
        self.current: int | None = None
        self._pick: Callable[[int], None] | None = None

    def choose_model(
        self, settings: Callable[[], Settings], then: Callable[[Settings], None]
    ) -> None:
        self.opened.append("model")
        _, self.current = model_choices(self.choices, settings())
        self._pick = lambda index: then(picked_model(self.choices, settings(), index))

    def choose_tools(
        self, settings: Callable[[], Settings], then: Callable[[Settings], None]
    ) -> None:
        self.opened.append("tools")
        _, self.current = tool_access_choices(settings())
        self._pick = lambda index: then(picked_tool_access(settings(), index))

    def pick(self, index: int) -> None:
        """Pick option ``index`` of the picker open, closing it."""
        assert self._pick is not None
        pick, self._pick = self._pick, None
        pick(index)

    def close(self) -> None:
        """Close the picker open, picking nothing, as escape does."""
        self._pick = None


def editing(
    task: TaskSpec | None, settings: Settings = DEFAULTS, pickers: Pickers | None = None
) -> tuple[TaskEditor, View, Pickers]:
    """An editor of ``task``, or a new one in a session with ``settings``, in a view whose
    fields start as the editor says."""
    view, pickers = View(), pickers or Pickers()
    editor = TaskEditor(view, task, settings, pickers)
    view.typed_title, view.typed_body = editor.text
    return editor, view, pickers


def test_a_new_task_starts_blank_with_the_sessions_settings_and_its_title_selected() -> None:
    editor, view, _ = editing(None, Settings(model="opus", tool_access="off"))
    assert editor.heading == "New task"
    assert (*editor.text, *editor.settings_shown) == ("", "", "opus", "off")
    assert editor.selected == "task-title"
    assert (view.selections, view.typing, view.dismissed) == ([], [], [])


def test_the_editor_builds_a_task_field_by_field() -> None:
    editor, view, pickers = editing(None)
    editor.insert()
    assert view.typing == ["title"]
    view.typed_title = "fix it"

    # j moves past the prompt to the model field; enter opens its picker.
    editor.move(1)
    editor.move(1)
    assert editor.selected == "task-model"
    assert view.selections == [("task-title", "task-prompt"), ("task-prompt", "task-model")]
    editor.choose()
    assert (pickers.opened, pickers.current) == (["model"], 0)
    pickers.pick(1)
    assert view.settings == ("opus", "restricted")

    editor.move(1)
    editor.choose()
    assert (pickers.opened[-1], pickers.current) == ("tools", 1)  # restricted
    pickers.pick(2)
    assert view.settings == ("opus", "full (auto-approved)")

    # i jumps back to the title field from the model or tools field.
    editor.insert()
    assert editor.selected == "task-title" and view.typing == ["title", "title"]
    assert view.selections[-1] == ("task-tools", "task-title")
    view.typed_title = "fix it!"
    editor.move(1)
    editor.move(1)
    editor.insert()
    assert editor.selected == "task-title" and view.typing[-1] == "title"
    assert view.dismissed == []

    # A title alone saves a single-line prompt, from whichever field is selected.
    for step in (-1, -1, 1, 1):
        editor.move(step)
    editor.save()
    task = TaskSpec(prompt="fix it!", settings=Settings(model="opus", tool_access="full"))
    assert view.dismissed == [task]
    assert task.role == "coder"


def test_moving_stops_at_the_first_and_last_fields() -> None:
    editor, view, _ = editing(None)
    editor.move(-1)
    assert editor.selected == "task-title"
    for _ in range(4):
        editor.move(1)
    assert editor.selected == "task-tools"
    assert view.selections == [
        ("task-title", "task-title"),
        ("task-title", "task-prompt"),
        ("task-prompt", "task-model"),
        ("task-model", "task-tools"),
        ("task-tools", "task-tools"),
    ]
    for _ in range(3):
        editor.move(-1)
    assert editor.selected == "task-title"


def test_cancelling_discards_the_task() -> None:
    editor, view, _ = editing(None)
    view.typed_title = "draft"
    editor.cancel()
    assert view.dismissed == [None]


def test_a_task_without_a_title_is_not_saved() -> None:
    editor, view, _ = editing(None)
    editor.save()
    view.typed_title = " "
    editor.save()
    # A body is no title.
    view.typed_body = "body"
    editor.save()
    assert view.dismissed == []


def test_the_editor_shows_a_tasks_model_and_tools_as_its_row_does() -> None:
    task = TaskSpec(prompt="Add a widget", settings=Settings(model="opus", tool_access="full"))
    editor, _, _ = editing(task)
    row_details = str(task_label(TaskState(id="1", task=task), "1", "")).split("\n")[1].strip()
    assert row_details == "opus · full (auto-approved)"
    assert " · ".join(editor.settings_shown) == row_details


def test_the_editor_starts_with_a_prompts_title_and_body_and_saves_the_body_typed() -> None:
    body = "Put it in widgets.py.\nTest it."
    editor, view, pickers = editing(TaskSpec(prompt=f"Add a widget\n\n{body}"))
    assert editor.heading == "Edit task"
    assert (*editor.text, *editor.settings_shown) == ("Add a widget", body, "default", "restricted")

    # j selects the prompt box, where there's nothing to choose; i types there.
    editor.move(1)
    assert editor.selected == "task-prompt"
    editor.choose()
    assert pickers.opened == []
    editor.insert()
    assert view.typing == ["body"]
    view.typed_body = f"{body}\nmore"
    editor.save()
    assert view.dismissed == [TaskSpec(prompt=f"Add a widget\n\n{body}\nmore")]


def test_saving_joins_the_title_and_the_body_typed() -> None:
    editor, view, _ = editing(None)
    view.typed_title, view.typed_body = "Title", "body"
    editor.save()
    view.typed_body = ""
    editor.save()
    assert view.dismissed == [TaskSpec(prompt="Title\n\nbody"), TaskSpec(prompt="Title")]


def test_saving_an_edited_task_takes_its_title_as_typed() -> None:
    editor, view, _ = editing(TaskSpec(prompt="one"))
    view.typed_title = "one!"
    editor.save()
    assert view.dismissed == [TaskSpec(prompt="one!")]


def test_a_fix_task_round_trips_through_the_editor_unchanged() -> None:
    editor, view, _ = editing(TaskSpec(prompt=FIX_PROMPT))
    editor.save()
    assert view.dismissed == [TaskSpec(prompt=FIX_PROMPT)]


def test_a_picker_closed_without_a_pick_keeps_the_value() -> None:
    editor, view, pickers = editing(None, Settings(model="sonnet"))
    editor.move(2)
    editor.choose()
    pickers.close()
    assert editor.settings_shown[0] == "sonnet" and view.settings is None
    # Enter on the title opens no picker.
    editor.move(-2)
    editor.choose()
    assert pickers.opened == ["model"]
    view.typed_title = "x"
    editor.save()
    assert view.dismissed == [TaskSpec(prompt="x", settings=Settings(model="sonnet"))]


def test_the_model_picker_opens_on_nothing_for_a_custom_model() -> None:
    editor, _, pickers = editing(None, Settings(model="claude-custom-1"))
    editor.move(2)
    editor.choose()
    assert pickers.opened == ["model"] and pickers.current is None
    pickers.close()
    assert editor.settings_shown[0] == "claude-custom-1"


def test_the_model_picker_offers_the_choices_it_has_when_opened() -> None:
    editor, view, pickers = editing(None)
    view.typed_title = "Add a widget"
    pickers.choices = ("opus", "sonnet", "haiku")
    editor.move(2)
    editor.choose()
    pickers.pick(3)
    editor.save()
    assert view.dismissed == [TaskSpec(prompt="Add a widget", settings=Settings(model="haiku"))]


def test_a_model_pick_takes_the_model_labelled_when_choices_change_while_open() -> None:
    editor, view, pickers = editing(None, Settings(tool_access="full"))
    view.typed_title = "Add a widget"
    editor.move(2)
    editor.choose()
    pickers.choices = ("haiku", "opus", "sonnet", "fable")
    pickers.pick(4)
    editor.save()
    task = TaskSpec(prompt="Add a widget", settings=Settings(model="fable", tool_access="full"))
    assert view.dismissed == [task]


def test_a_tool_access_pick_changes_only_the_tool_access() -> None:
    editor, view, pickers = editing(None, Settings(model="opus", tool_access="off"))
    view.typed_title = "Add a widget"
    editor.move(2)
    editor.choose()
    assert pickers.current == 1
    pickers.pick(2)
    editor.move(1)
    editor.choose()
    assert pickers.current == 0
    pickers.pick(2)
    editor.save()
    task = TaskSpec(prompt="Add a widget", settings=Settings(model="sonnet", tool_access="full"))
    assert view.dismissed == [task]


def test_the_editor_opens_the_pickers_it_is_given() -> None:
    pickers = Pickers()
    editor, _, _ = editing(None, pickers=pickers)
    editor.move(2)
    editor.choose()
    editor.move(1)
    editor.choose()
    assert pickers.opened == ["model", "tools"]


def test_editing_a_task_changes_what_is_typed_and_picked() -> None:
    editor, view, pickers = editing(TaskSpec(prompt="two"))
    assert editor.heading == "Edit task" and editor.text[0] == "two"
    view.typed_title = "two!"
    for _ in range(3):
        editor.move(1)
    editor.choose()
    pickers.pick(0)
    editor.save()
    assert view.dismissed == [TaskSpec(prompt="two!", settings=Settings(tool_access="off"))]


def test_editing_a_task_keeps_its_role() -> None:
    editor, view, _ = editing(TaskSpec(prompt="write it", role="writer"))
    view.typed_title = "write it!"
    editor.save()
    assert view.dismissed == [TaskSpec(prompt="write it!", role="writer")]
