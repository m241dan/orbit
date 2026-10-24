"""The pickers that change a model and tool access, shared by the app and the task editor."""

from collections.abc import Sequence

from textual.app import App

from orbit.tui.view.settings import (
    MODEL_TITLE,
    TOOL_ACCESS_TITLE,
    Chosen,
    Pickers,
    model_options,
    tool_access_labels,
)
from orbit.tui.widgets import PickerScreen


class SettingsPickers:
    """The model and tool access pickers, as their :class:`Pickers` model has them, built once
    and installed so they open instantly."""

    def __init__(self) -> None:
        self.model_picker = PickerScreen(MODEL_TITLE, model_options(())[1])
        self.tool_picker = PickerScreen(TOOL_ACCESS_TITLE, tool_access_labels())
        self.model = Pickers(self)

    def install(self, app: App[None]) -> None:
        """Install the pickers in ``app``."""
        app.install_screen(self.model_picker, "pick-model")
        app.install_screen(self.tool_picker, "pick-tool-access")

    def label_models(self, labels: Sequence[str]) -> None:
        self.model_picker.set_labels(labels)

    def pick_model(self, current: int | None, chosen: Chosen) -> None:
        self.model_picker.choose(current, chosen)

    def pick_tool_access(self, current: int | None, chosen: Chosen) -> None:
        self.tool_picker.choose(current, chosen)
