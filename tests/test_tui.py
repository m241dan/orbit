import asyncio
from collections.abc import AsyncIterator

import pytest
from textual.pilot import Pilot
from textual.widgets import Footer, Input, OptionList

from orbit.protocol import (
    AssistantMessage,
    CancelTurn,
    Command,
    ErrorOccurred,
    Event,
    Settings,
    SettingsChanged,
    SubmitPrompt,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
)
from orbit.tui import OrbitApp
from orbit.tui.widgets import Entry, PickerScreen, PromptInput, SidePanel, Transcript


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
        await pilot.press("i", *"hi", "enter")
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
        await pilot.press("i", "space", "enter")
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
        # The side panels take 33% (26 columns); conversation and prompt get the rest.
        assert transcript.region.width == prompt.region.width == 54
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


async def test_prompt_starts_in_normal_mode_and_ignores_typing_and_clicks() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        assert app.focused is None
        await pilot.press(*"hello")
        await pilot.click("#prompt")
        assert app.focused is None
        assert prompt.value == ""


async def test_i_enters_insert_mode_without_typing_the_i() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        await pilot.press("i")
        assert app.focused is prompt
        await pilot.press(*"hi")
        assert prompt.value == "hi"


async def test_escape_in_insert_mode_returns_to_normal_mode_without_cancelling() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        await pilot.press("i", "a", "escape")
        assert app.focused is None
        assert session.commands == []

        # Back in normal mode: keys don't type, the draft is kept, escape cancels.
        await pilot.press("b", "escape")
        assert prompt.value == "a"
        assert session.commands == [CancelTurn()]

        await pilot.press("i", "b")
        assert prompt.value == "ab"


async def test_submit_returns_to_normal_mode_and_finished_turn_stays_there() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("i", *"hi", "enter")
        assert app.focused is None

        await session.queue.put(TurnStarted(turn_id="t"))
        await pilot.pause()
        await pilot.press("i")
        assert app.focused is None  # can't edit while a turn runs

        await session.queue.put(TurnFinished(turn_id="t", reason="completed"))
        await pilot.pause()
        assert app.focused is None
        await pilot.press("i")
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


CHOICES = ("opus", "sonnet")


async def show_settings(session: FakeSession, pilot: Pilot[None], settings: Settings) -> None:
    await session.queue.put(SettingsChanged(settings=settings, model_choices=CHOICES))
    await pilot.pause()


async def test_settings_shown_under_prompt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        assert not app.query_one(PromptInput).border_subtitle

        await show_settings(session, pilot, Settings())
        assert prompt.border_subtitle == "model: default · tools: restricted"

        await show_settings(session, pilot, Settings(model="opus", tool_access="full"))
        assert prompt.border_subtitle == "model: opus · tools: full (auto-approved)"

        await show_settings(session, pilot, Settings(tool_access="off"))
        assert prompt.border_subtitle == "model: default · tools: off"


async def test_settings_keys_do_nothing_before_backend_reports_settings() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+o", "ctrl+l")
        assert session.commands == []
        assert not isinstance(app.screen, PickerScreen)


async def test_display_waits_for_backend_to_confirm_settings() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        prompt = app.query_one(PromptInput)
        await pilot.press("ctrl+l", "down", "enter")
        await pilot.pause()
        assert prompt.border_subtitle == "model: default · tools: restricted"
        assert session.commands == [UpdateSettings(settings=Settings(tool_access="full"))]

        await show_settings(session, pilot, Settings(tool_access="full"))
        assert prompt.border_subtitle == "model: default · tools: full (auto-approved)"


def picker(app: OrbitApp) -> tuple[str, list[str], int | None]:
    """The open picker's title, option labels and highlighted index."""
    screen = app.screen
    assert isinstance(screen, PickerScreen)
    options = screen.query_one(OptionList)
    labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
    return screen.title_text, labels, options.highlighted


async def test_ctrl_o_lists_models_with_current_highlighted() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus"))
        await pilot.press("ctrl+o")
        assert picker(app) == ("Model", ["default", "opus", "sonnet"], 1)


async def test_model_picker_selection_requests_that_model() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus", tool_access="full"))
        await pilot.press("ctrl+o", "down", "enter")
        assert session.commands == [
            UpdateSettings(settings=Settings(model="sonnet", tool_access="full"))
        ]
        assert not isinstance(app.screen, PickerScreen)


async def test_model_picker_highlights_nothing_for_custom_model() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="claude-custom-1"))
        await pilot.press("ctrl+o")
        assert picker(app)[2] is None


async def test_ctrl_l_lists_tool_access_and_selects() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus", tool_access="off"))
        await pilot.press("ctrl+l")
        assert picker(app) == (
            "Tool access",
            [
                "off: no tools",
                "restricted: read-only; anything needing approval is refused",
                "full: auto-approve everything",
            ],
            0,
        )

        await pilot.press("down", "down", "enter")
        assert session.commands == [
            UpdateSettings(settings=Settings(model="opus", tool_access="full"))
        ]


async def test_escape_closes_picker_without_changes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await pilot.press("ctrl+l", "down", "escape")
        assert not isinstance(app.screen, PickerScreen)
        assert session.commands == []


async def test_pickers_are_built_once_and_reused() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        opened: list[object] = []
        for key in ["ctrl+o", "ctrl+o", "ctrl+l", "ctrl+l"]:
            await pilot.press(key)
            opened.append(app.screen)
            await pilot.press("escape")

        assert opened[0] is opened[1]
        assert opened[2] is opened[3]
        # A later settings report with the same choices keeps the cached picker.
        await show_settings(session, pilot, Settings(model="opus"))
        await pilot.press("ctrl+o")
        assert app.screen is opened[0]
        assert picker(app)[2] == 1


async def test_model_picker_updates_when_choices_change() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await pilot.press("ctrl+o")
        first = app.screen
        await pilot.press("escape")

        await session.queue.put(SettingsChanged(settings=Settings(), model_choices=("haiku",)))
        await pilot.pause()
        await pilot.press("ctrl+o")
        assert app.screen is first
        assert picker(app)[1] == ["default", "haiku"]


async def test_picker_keys_ignored_while_a_picker_is_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await pilot.press("ctrl+o")
        model_picker = app.screen
        await pilot.press("ctrl+o", "ctrl+l")
        assert app.screen is model_picker
        assert len(app.screen_stack) == 2


async def test_ctrl_c_quits_even_with_prompt_focused() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("i")
        assert app.focused is app.query_one(PromptInput)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running


async def test_ctrl_q_does_not_quit() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running


async def test_side_panels_fill_right_third_top_and_bottom() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(120, 41)) as pilot:
        await pilot.pause()
        tasks = app.query_one("#task-list", SidePanel)
        steps = app.query_one("#verification-steps", SidePanel)
        transcript = app.query_one(Transcript).region
        prompt = app.query_one(PromptInput).region
        assert (tasks.border_title, steps.border_title) == ("Task list", "Verification steps")

        # 33% of the width (39.6 rounds down), split top/bottom above the footer.
        for panel in (tasks, steps):
            assert panel.region.width == 39
            assert panel.region.right == 120
        assert tasks.region.y == 0
        assert steps.region.y == tasks.region.bottom
        assert steps.region.bottom == 40
        assert tasks.region.height == steps.region.height

        # Conversation and prompt take the rest of the width on the left.
        assert transcript.width == prompt.width == 81
        assert prompt.bottom == 40


def selected(app: OrbitApp) -> list[str | None]:
    return [w.id for w in app.query(".selected")]


async def test_prompt_window_is_selected_on_start_and_highlighted() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert selected(app) == ["prompt"]
        assert app.focused is None
        tasks = app.query_one("#task-list")
        unselected_border = tasks.styles.border_top
        tasks.add_class("selected")
        assert tasks.styles.border_top != unselected_border


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("k", "transcript"),
        ("l", "verification-steps"),
        ("kl", "task-list"),
        ("klj", "verification-steps"),
        ("kljh", "prompt"),
        ("klh", "transcript"),
        ("kj", "prompt"),
        ("lk", "task-list"),
        # Edges: nothing further in that direction.
        ("j", "prompt"),
        ("h", "prompt"),
        ("kk", "transcript"),
        ("kh", "transcript"),
        ("kll", "task-list"),
        ("lj", "verification-steps"),
    ],
)
async def test_hjkl_moves_selection_between_windows(keys: str, expected: str) -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press(*keys)
        assert selected(app) == [expected]
        assert app.focused is None


async def test_border_highlight_follows_selection() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        tasks = app.query_one("#task-list")
        before = tasks.styles.border_top
        await pilot.press("k", "l")
        assert tasks.styles.border_top != before
        await pilot.press("h")
        assert tasks.styles.border_top == before


async def test_enter_does_nothing_off_the_prompt() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("k", "enter")
        assert app.focused is None
        assert selected(app) == ["transcript"]


@pytest.mark.parametrize("keys", ["k", "kl", "l", "lk"])
async def test_i_jumps_to_prompt_from_any_window(keys: str) -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press(*keys, "i")
        assert selected(app) == ["prompt"]
        assert app.focused is app.query_one(PromptInput)

        await pilot.press("escape")
        assert selected(app) == ["prompt"]
        assert app.focused is None


async def test_enter_on_prompt_does_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.focused is None
        assert session.commands == []


async def test_i_on_prompt_enters_insert_mode_and_hjkl_type() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        normal_border = prompt.styles.border_top
        await pilot.press("k", "j", "i")
        assert app.focused is prompt
        assert prompt.styles.border_top != normal_border  # active, not just selected
        await pilot.press(*"hjkl")
        assert prompt.value == "hjkl"
        assert selected(app) == ["prompt"]

        await pilot.press("escape", "k")
        assert app.focused is None
        assert selected(app) == ["transcript"]
