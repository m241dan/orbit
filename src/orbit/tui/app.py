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
    Settings,
    SettingsChanged,
    SubmitPrompt,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
)
from orbit.tui.widgets import PromptInput, Transcript

NEXT_TOOL_ACCESS: dict[ToolAccess, ToolAccess] = {
    "off": "restricted",
    "restricted": "full",
    "full": "off",
}


def next_model(current: str | None, choices: tuple[str, ...]) -> str | None:
    """Cycle default -> each choice -> default. Unknown models go back to default."""
    options = (None, *choices)
    if current not in options:
        return None
    return options[(options.index(current) + 1) % len(options)]


class OrbitApp(App[None]):
    TITLE = "orbit"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel_turn", "Cancel turn"),
        Binding("f2", "next_model", "Model"),
        Binding("f3", "next_tool_access", "Tools"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session
        # Last settings reported by the backend; None until the first report.
        self._settings: Settings | None = None
        self._model_choices: tuple[str, ...] = ()

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

    async def action_next_model(self) -> None:
        if self._settings is not None:
            model = next_model(self._settings.model, self._model_choices)
            await self._request_settings(self._settings.model_copy(update={"model": model}))

    async def action_next_tool_access(self) -> None:
        if self._settings is not None:
            access = NEXT_TOOL_ACCESS[self._settings.tool_access]
            await self._request_settings(self._settings.model_copy(update={"tool_access": access}))

    async def _request_settings(self, settings: Settings) -> None:
        # The display updates when the backend confirms with SettingsChanged.
        await self._session.send(UpdateSettings(settings=settings))

    @work(exclusive=True, group="events")
    async def _consume_events(self) -> None:
        async for event in self._session.events():
            self._render_event(event)

    def _render_event(self, event: Event) -> None:
        match event:
            case SettingsChanged(settings=settings, model_choices=choices):
                self._settings, self._model_choices = settings, choices
                self.query_one(PromptInput).show_settings(settings)
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
