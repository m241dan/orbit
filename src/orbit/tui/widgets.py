"""Building blocks of the orbit screen."""

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from orbit.protocol import Settings, ToolAccess

TOOL_ACCESS_LABELS: dict[ToolAccess, str] = {
    "off": "off",
    "restricted": "restricted",
    "full": "full (auto-approved)",
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
