from textual import events, on
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets.text_area import Location

from orbit.tui.widgets import ChatPrompt

WIDTH = 40  # 36 columns of text inside the border and padding.


class ChatApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def compose(self) -> ComposeResult:
        yield ChatPrompt(placeholder="Press i to type", id="prompt")

    @on(ChatPrompt.Submitted)
    def _sent(self, message: ChatPrompt.Submitted) -> None:
        self.sent.append(message.text)
        message.prompt.clear()


def rows(prompt: ChatPrompt) -> int:
    """The rows of text the prompt shows, inside its border."""
    return prompt.region.height - 2


async def delete_back_to(pilot: Pilot[None], prompt: ChatPrompt, end: Location) -> int:
    """The prompt's rows after cutting its text back to ``end`` directly, before deleting one
    more character by pressing backspace, as the user would."""
    prompt.delete(end, prompt.document.end)
    prompt.move_cursor(end)
    await pilot.pause()
    cut = rows(prompt)
    await pilot.press("backspace")
    await pilot.pause()
    return cut


async def test_starts_one_row_high_in_normal_mode() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        assert rows(prompt) == 1
        await pilot.press(*"hi")
        assert prompt.text == ""
        assert app.focused is None


async def test_a_long_line_wraps_and_grows_the_box() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        prompt.insert("word " * 12)
        await pilot.pause()
        assert "\n" not in prompt.text
        assert rows(prompt) == 2


async def test_grows_up_to_eight_rows_then_scrolls() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        prompt.insert("word " * 50)
        await pilot.pause()
        assert rows(prompt) == 8
        assert prompt.max_scroll_y > 0


async def test_pasted_newlines_are_kept_and_grow_the_box() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        await pilot.pause()
        app.post_message(events.Paste("one\ntwo\nthree"))
        await pilot.pause()
        assert prompt.text == "one\ntwo\nthree"
        assert rows(prompt) == 3


async def test_enter_sends_the_whole_text_then_clears_and_shrinks() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        app.post_message(events.Paste("one\ntwo"))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.sent == ["one\ntwo"]
        assert prompt.text == ""
        assert rows(prompt) == 1


async def test_escape_keeps_the_draft_and_returns_to_normal_mode() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        await pilot.press("a", "escape", "b")
        assert app.focused is None
        assert prompt.text == "a"
        assert app.sent == []
        prompt.enter_insert_mode()
        await pilot.press("b")
        assert prompt.text == "ab"


async def test_insert_mode_does_nothing_while_disabled() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.disabled = True
        prompt.enter_insert_mode()
        await pilot.pause()
        assert app.focused is None


async def test_shrinks_as_wrapped_text_is_deleted() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        prompt.insert("word " * 50)
        await pilot.pause()
        assert rows(prompt) == 8
        assert await delete_back_to(pilot, prompt, (0, 71)) == 3
        assert rows(prompt) == 2
        await delete_back_to(pilot, prompt, (0, 6))
        assert prompt.text == "word "
        assert rows(prompt) == 1
        assert prompt.max_scroll_y == 0


async def test_shrinks_as_pasted_lines_are_deleted() -> None:
    app = ChatApp()
    async with app.run_test(size=(WIDTH, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        prompt.enter_insert_mode()
        await pilot.pause()
        app.post_message(events.Paste("one\ntwo\nthree"))
        await pilot.pause()
        assert rows(prompt) == 3
        await delete_back_to(pilot, prompt, (1, 0))
        assert prompt.text == "one"
        assert rows(prompt) == 1
