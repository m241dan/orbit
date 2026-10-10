"""Textual frontend. Talks to the backend only through a protocol Session."""

import json
from typing import ClassVar, assert_never

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Input

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
from orbit.tui.widgets import PromptInput, Transcript


class OrbitApp(App[None]):
    TITLE = "orbit"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel_turn", "Cancel turn")]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        yield Transcript(id="transcript")
        yield PromptInput(placeholder="Ask orbit…", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(PromptInput).focus()
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
                self.query_one(PromptInput).disabled = True
            case AssistantMessage(text=text):
                self._append(text, "assistant")
            case ToolCallStarted(name=name, arguments=arguments):
                self._append(f"→ {name} {json.dumps(arguments)}", "tool")
            case ToolCallFinished(output=output, is_error=is_error):
                self._append(output, "error" if is_error else "tool")
            case TurnFinished(reason=reason):
                if reason not in ("completed", "error"):
                    self._append(f"[turn ended: {reason}]", "tool")
                prompt = self.query_one(PromptInput)
                prompt.disabled = False
                prompt.focus()
            case ErrorOccurred(message=message):
                self._append(message, "error")
            case _:
                assert_never(event)

    def _append(self, text: str, kind: str) -> None:
        self.query_one(Transcript).add_entry(text, kind)
