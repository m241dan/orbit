"""The pickers that change a model and tool access, shared by the app and the task editor."""

from collections.abc import Awaitable, Callable

from textual.app import App

from orbit.protocol import Settings
from orbit.tui.view.settings import (
    MODEL_TITLE,
    TOOL_ACCESS_TITLE,
    model_choices,
    model_options,
    picked_model,
    picked_tool_access,
    tool_access_choices,
    tool_access_labels,
)
from orbit.tui.widgets import PickerScreen

Picked = Callable[[Settings], Awaitable[None] | None]


class SettingsPickers:
    """The model and tool access pickers, built once and installed so they open instantly, and
    the model choices the model picker offers, its labels following them.

    The settings may change while a picker is open, so a pick changes only its own setting of
    the settings as they are when picked.
    """

    def __init__(self) -> None:
        self.model_picker = PickerScreen(MODEL_TITLE, model_options(())[1])
        self.tool_picker = PickerScreen(TOOL_ACCESS_TITLE, tool_access_labels())
        self.model_choices: tuple[str, ...] = ()

    def install(self, app: App[None]) -> None:
        """Install the pickers in ``app``."""
        app.install_screen(self.model_picker, "pick-model")
        app.install_screen(self.tool_picker, "pick-tool-access")

    def show_choices(self, choices: tuple[str, ...]) -> None:
        """Offer the models ``choices``, relabelling the model picker if they changed."""
        if choices != self.model_choices:
            self.model_picker.set_labels(model_options(choices)[1])
        self.model_choices = choices

    def choose_model(self, settings: Callable[[], Settings], then: Picked) -> None:
        """Open the model picker on the model of ``settings``, the settings now, and call
        ``then`` with them, as they are when picked, with the model picked."""
        _, current = model_choices(self.model_choices, settings())
        self.model_picker.choose(
            current, lambda index: then(picked_model(self.model_choices, settings(), index))
        )

    def choose_tools(self, settings: Callable[[], Settings], then: Picked) -> None:
        """Open the tool access picker on the tool access of ``settings``, the settings now, and
        call ``then`` with them, as they are when picked, with the tool access picked."""
        _, current = tool_access_choices(settings())
        self.tool_picker.choose(current, lambda index: then(picked_tool_access(settings(), index)))
