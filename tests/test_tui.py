import asyncio
from collections.abc import AsyncIterator

from textual.widgets import Footer, Input

from orbit.protocol import (
    AssistantMessage,
    CancelTurn,
    Command,
    ErrorOccurred,
    Event,
    SubmitPrompt,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.widgets import Entry, PromptInput, Transcript


class FakeSession:
    """Stands in for the backend: records commands, replays scripted events."""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.queue: asyncio.Queue[Event] = asyncio.Queue()

    async def send(self, command: Command) -> None:
        self.commands.append(command)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()


async def test_submit_sends_command_and_renders_events() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        assert session.commands == [SubmitPrompt(text="hi")]

        await session.queue.put(TurnStarted(turn_id="t"))
        await pilot.pause()
        assert app.query_one("#prompt", Input).disabled

        await session.queue.put(AssistantMessage(turn_id="t", text="hello back"))
        await session.queue.put(TurnFinished(turn_id="t", reason="completed"))
        await pilot.pause()

        texts = [str(entry.render()) for entry in app.query(Entry)]
        assert texts == ["hi", "hello back"]
        assert not app.query_one("#prompt", Input).disabled


async def test_escape_sends_cancel() -> None:
    session = FakeSession()
    async with OrbitApp(session).run_test() as pilot:
        await pilot.press("escape")
        assert session.commands == [CancelTurn()]


async def test_blank_submit_is_ignored() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.click("#prompt")
        await pilot.press("space", "enter")
        assert session.commands == []
        assert len(app.query(Entry)) == 0


async def test_renders_tool_calls_errors_and_cancellation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        events: list[Event] = [
            ToolCallStarted(turn_id="t", call_id="c1", name="upper", arguments={"text": "hi"}),
            ToolCallFinished(turn_id="t", call_id="c1", output="HI"),
            ToolCallFinished(turn_id="t", call_id="c2", output="KeyError", is_error=True),
            ErrorOccurred(message="boom"),
            TurnFinished(turn_id="t", reason="cancelled"),
        ]
        for event in events:
            await session.queue.put(event)
        await pilot.pause()

        rendered = [(str(e.render()), e.classes) for e in app.query(Entry)]
        assert rendered == [
            ('→ upper {"text": "hi"}', frozenset({"tool"})),
            ("HI", frozenset({"tool"})),
            ("KeyError", frozenset({"error"})),
            ("boom", frozenset({"error"})),
            ("[turn ended: cancelled]", frozenset({"tool"})),
        ]


async def test_layout_stacks_display_over_prompt_over_footer() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(80, 24)):
        transcript = app.query_one(Transcript)
        prompt = app.query_one(PromptInput)
        footer = app.query_one(Footer)

        assert transcript.region.y == 0
        assert transcript.region.bottom == prompt.region.y
        assert prompt.region.bottom == footer.region.y
        assert footer.region.bottom == 24
        assert transcript.region.width == prompt.region.width == 80
        assert prompt.region.height == 3  # one line of text inside a border


async def test_display_absorbs_resize_and_prompt_stays_at_bottom() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        prompt = app.query_one(PromptInput)
        footer = app.query_one(Footer)
        before = transcript.region.height

        await pilot.resize_terminal(100, 40)
        await pilot.pause()

        assert transcript.region.height == before + 16
        assert prompt.region.height == 3
        assert prompt.region.bottom == footer.region.y


async def test_sections_are_labelled() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert app.query_one(Transcript).border_title == "Conversation"
        assert app.query_one(PromptInput).border_title == "Prompt"


async def test_prompt_has_focus_on_start() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert app.focused is app.query_one(PromptInput)


async def test_display_follows_latest_output() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        for i in range(40):
            await session.queue.put(AssistantMessage(turn_id="t", text=f"line {i}"))
        await pilot.pause()

        transcript = app.query_one(Transcript)
        assert transcript.max_scroll_y > 0
        assert transcript.scroll_y == transcript.max_scroll_y
