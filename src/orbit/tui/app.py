"""Textual frontend. Talks to the backend only through a protocol Session."""

import json
from typing import ClassVar, assert_never

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from orbit.protocol import (
    AssistantMessage,
    CancelTurn,
    ErrorOccurred,
    Event,
    Session,
    SubmitPrompt,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)


class Entry(Static):
    """One line in the transcript."""

    DEFAULT_CSS = """
    Entry { margin: 0 0 1 0; }
    Entry.user { color: $text-accent; }
    Entry.tool { color: $text-muted; }
    Entry.error { color: $text-error; }
    """

    def __init__(self, text: str, kind: str) -> None:
        super().__init__(Text(text), classes=kind)


class OrbitApp(App[None]):
    TITLE = "orbit"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel_turn", "Cancel turn")]
    CSS = """
    #transcript { height: 1fr; padding: 0 1; }
    #prompt { dock: bottom; }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield Input(placeholder="Ask orbit…", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._consume_events()

    @on(Input.Submitted, "#prompt")
    async def _submit(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        if not text:
            return
        message.input.clear()
        self._append(text, "user")
        await self._session.send(SubmitPrompt(text=text))

    async def action_cancel_turn(self) -> None:
        await self._session.send(CancelTurn())

    @work(exclusive=True, group="events")
    async def _consume_events(self) -> None:
        async for event in self._session.events():
            self._render_event(event)

    def _render_event(self, event: Event) -> None:
        match event:
            case TurnStarted():
                self.query_one("#prompt", Input).disabled = True
            case AssistantMessage(text=text):
                self._append(text, "assistant")
            case ToolCallStarted(name=name, arguments=arguments):
                self._append(f"→ {name} {json.dumps(arguments)}", "tool")
            case ToolCallFinished(output=output, is_error=is_error):
                self._append(output, "error" if is_error else "tool")
            case TurnFinished(reason=reason):
                if reason not in ("completed", "error"):
                    self._append(f"[turn ended: {reason}]", "tool")
                prompt = self.query_one("#prompt", Input)
                prompt.disabled = False
                prompt.focus()
            case ErrorOccurred(message=message):
                self._append(message, "error")
            case _:
                assert_never(event)

    def _append(self, text: str, kind: str) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.mount(Entry(text, kind))
        transcript.scroll_end(animate=False)
