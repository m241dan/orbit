"""Building blocks of the orbit screen."""

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from orbit.protocol import Settings, ToolAccess

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


class Entry(Static):
    """One item in the transcript."""

    DEFAULT_CSS = """
    Entry { margin: 0 0 1 0; }
    Entry.user { color: $text-accent; text-style: bold; }
    Entry.tool { color: $text-muted; }
    Entry.error { color: $text-error; }
    """

    def __init__(self, text: str, kind: str) -> None:
        super().__init__(Text(text), classes=kind)


class Transcript(VerticalScroll):
    """Main display: the running conversation, pinned to the latest output."""

    DEFAULT_CSS = """
    Transcript {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "Conversation"

    def add_entry(self, text: str, kind: str) -> None:
        self.mount(Entry(text, kind))
        self.scroll_end(animate=False)


class SidePanel(VerticalScroll):
    """Titled pane in the right-hand column."""

    DEFAULT_CSS = """
    SidePanel {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, *, id: str) -> None:
        super().__init__(id=id)
        self.border_title = title


class PromptInput(Input):
    """Single-line prompt box at the bottom of the screen."""

    DEFAULT_CSS = """
    PromptInput {
        height: 3;
        border: round $secondary;
    }
    PromptInput:focus { border: round $accent; }
    """

    def on_mount(self) -> None:
        self.border_title = "Prompt"

    def show_settings(self, settings: Settings) -> None:
        model = settings.model or "default"
        tools = TOOL_ACCESS_LABELS[settings.tool_access]
        self.border_subtitle = f"model: {model} · tools: {tools}"


class PickerScreen(ModalScreen[int | None]):
    """Pop-up list of options. Dismisses with the chosen index, or None on escape.

    Meant to be built once and installed so reopening it is instant.
    """

    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > OptionList {
        width: 70;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        padding: 0 1;
    }
    PickerScreen > OptionList, PickerScreen > OptionList:focus { border: round $accent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]

    def __init__(self, title_text: str, labels: Sequence[str]) -> None:
        super().__init__()
        self.title_text = title_text
        self._labels = list(labels)
        # Index to highlight each time the picker is shown.
        self.current: int | None = None

    def compose(self) -> ComposeResult:
        options = OptionList(*self._labels)
        options.border_title = self.title_text
        yield options

    def set_labels(self, labels: Sequence[str]) -> None:
        self._labels = list(labels)
        if self.is_mounted:
            options = self.query_one(OptionList)
            options.clear_options()
            options.add_options(self._labels)

    def on_screen_resume(self) -> None:
        self.query_one(OptionList).highlighted = self.current

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def action_close(self) -> None:
        self.dismiss(None)
