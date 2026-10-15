import asyncio
from collections.abc import AsyncIterator

import pytest
from textual.pilot import Pilot
from textual.widgets import Footer, Input, OptionList, Tab

from orbit.protocol import (
    AgentExited,
    AgentReply,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    Command,
    ErrorOccurred,
    Event,
    Notice,
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
from orbit.tui.tasks import TaskList
from orbit.tui.view import spinner_frame
from orbit.tui.widgets import (
    ConversationTabs,
    Entry,
    PickerScreen,
    PromptInput,
    SidePanel,
    Transcript,
)


class FakeSession:
    """Stands in for the backend: records commands, replays scripted events."""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.closed = False

    async def send(self, command: Command) -> None:
        self.commands.append(command)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()

    async def close(self) -> None:
        self.closed = True


async def test_quitting_closes_the_session() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+c")
    assert session.closed


async def test_submit_sends_command_and_renders_events() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        await pilot.press("i", *"hi", "enter")
        assert session.commands == [SubmitPrompt(text="hi")]
        # Busy from now, not from the backend's notice, so a second prompt can't race it.
        assert prompt.disabled

        for event in (
            BusyChanged(busy=True, lane="planner"),
            TurnStarted(turn_id="t"),
            AssistantMessage(turn_id="t", text="hello back"),
            TurnFinished(turn_id="t", reason="completed"),
        ):
            await session.queue.put(event)
        await pilot.pause()
        texts = [str(entry.render()) for entry in app.query(Entry)]
        assert texts == ["hi", "hello back"]
        assert prompt.disabled

        await session.queue.put(BusyChanged(busy=False, lane="planner"))
        await pilot.pause()
        assert not prompt.disabled


async def test_a_busy_planner_disables_the_prompt() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await session.queue.put(BusyChanged(busy=True, lane="planner"))
        await pilot.pause()
        assert app.query_one(PromptInput).disabled


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


async def test_renders_agent_activity() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await session.queue.put(AgentStarted(agent_id="agent-1", role="coder", task_id="1"))
        await session.queue.put(AgentReply(agent_id="agent-1", request_id="r", text="Do X."))
        await session.queue.put(Notice(message="1 stash"))
        await pilot.pause()

        rendered = [(str(e.render()), e.classes) for e in app.query(Entry)]
        assert rendered == [
            ("▶ agent-1 (coder) started", frozenset({"agent"})),
            ("orbit → agent-1: Do X.", frozenset({"agent"})),
            ("📌 1 stash", frozenset({"verify"})),
        ]


async def test_layout_stacks_display_over_prompt_over_footer() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(80, 24)):
        tabs = app.query_one(ConversationTabs)
        transcript = app.query_one(Transcript)
        prompt = app.query_one(PromptInput)
        footer = app.query_one(Footer)

        assert tabs.region.y == 0
        assert tabs.region.bottom == transcript.region.y
        assert tabs.region.width == transcript.region.width
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
        assert app.query_one(PromptInput).border_title == "Prompt · planner"
        assert "planner" in app.query_one(PromptInput).placeholder


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

        await session.queue.put(BusyChanged(busy=True, lane="planner"))
        await pilot.pause()
        await pilot.press("i")
        assert app.focused is None  # can't edit while the planner answers

        await session.queue.put(BusyChanged(busy=False, lane="planner"))
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
        assert prompt.border_subtitle == "model: default · new tasks' tools: restricted"

        await show_settings(session, pilot, Settings(model="opus", tool_access="full"))
        assert prompt.border_subtitle == "model: opus · new tasks' tools: full (auto-approved)"

        await show_settings(session, pilot, Settings(tool_access="off"))
        assert prompt.border_subtitle == "model: default · new tasks' tools: off"


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
        assert prompt.border_subtitle == "model: default · new tasks' tools: restricted"
        assert session.commands == [UpdateSettings(settings=Settings(tool_access="full"))]

        await show_settings(session, pilot, Settings(tool_access="full"))
        assert prompt.border_subtitle == "model: default · new tasks' tools: full (auto-approved)"


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
        tasks = app.query_one("#task-list")
        steps = app.query_one("#verification-steps", SidePanel)
        transcript = app.query_one(Transcript).region
        prompt = app.query_one(PromptInput).region
        assert (tasks.border_title, steps.border_title) == ("Task list", "Verification steps")

        agents = app.query_one("#agents")
        # 33% of the width (39.6 rounds down): the task list and steps share the height above
        # the agents pane, which sits at the bottom, above the footer.
        for panel in (tasks, steps, agents):
            assert panel.region.width == 39
            assert panel.region.right == 120
        assert tasks.region.y == 0
        assert steps.region.y == tasks.region.bottom
        assert agents.region.y == steps.region.bottom
        assert agents.region.bottom == 40
        assert abs(tasks.region.height - steps.region.height) <= 1  # An odd row goes to one.

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
        ("lj", "agents"),
        ("ljk", "verification-steps"),
        ("ljh", "prompt"),
        ("ljj", "agents"),
        ("ljl", "agents"),
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


async def test_pickers_move_with_j_and_k() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus", tool_access="off"))
        await pilot.press("ctrl+o", "j")
        assert picker(app)[2] == 2
        await pilot.press("k", "k")
        assert picker(app)[2] == 0
        await pilot.press("escape")
        # The window selection underneath didn't move.
        assert selected(app) == ["prompt"]

        await pilot.press("ctrl+l", "j", "j", "k", "enter")
        assert session.commands == [
            UpdateSettings(settings=Settings(model="opus", tool_access="restricted"))
        ]


def test_spinner_cycles_the_arc_eight_frames_a_second() -> None:
    frames = [spinner_frame(i / 8) for i in range(7)]
    # Padded to two columns, like the emoji it stands beside.
    assert frames == ["◜ ", "◠ ", "◝ ", "◞ ", "◡ ", "◟ ", "◜ "]


async def feed_events(session: FakeSession, pilot: Pilot[None], *events: Event) -> None:
    for event in events:
        await session.queue.put(event)
    await pilot.pause()


def tab_strip(app: OrbitApp) -> list[str]:
    return [tab.label_text for tab in app.query_one(ConversationTabs).query(Tab)]


def shown(app: OrbitApp) -> list[str]:
    return [str(e.render()) for e in app.query(Entry) if e.display]


def started(agent_id: str, role: str = "coder") -> AgentStarted:
    return AgentStarted(agent_id=agent_id, role=role, task_id="1")


def said(agent_id: str, text: str) -> AssistantMessage:
    return AssistantMessage(turn_id="t", text=text, agent_id=agent_id)


async def test_the_tab_strip_starts_with_all_and_planner_and_never_takes_focus() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        tabs = app.query_one(ConversationTabs)
        assert tab_strip(app) == ["All", "Planner"]
        assert tabs.active_tab is not None and tabs.active_tab.label_text == "All"
        assert not tabs.focusable
        assert app.query_one(Transcript).border_title == "Conversation"
        await pilot.click(Tab)
        assert app.focused is None
        # h/j/k/l still move straight between the conversation and the prompt.
        await pilot.press("k")
        assert app.query_one(Transcript).has_class("selected")
        await pilot.press("j")
        assert app.query_one(PromptInput).has_class("selected")


async def test_each_started_agent_adds_a_tab_labelled_by_role() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(
            session, pilot, started("agent-1"), started("agent-2", "reviewer"), started("agent-3")
        )
        assert tab_strip(app) == ["All", "Planner", "coder", "reviewer", "coder 2"]
        assert app.query_one(Transcript).only is None
        await feed_events(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        assert tab_strip(app) == ["All", "Planner", "reviewer", "coder"]
        assert app.query_one(Transcript).border_title == "Conversation"


async def test_clicking_a_tab_shows_only_that_agents_conversation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session,
            pilot,
            AssistantMessage(turn_id="t", text="chat"),
            started("agent-1"),
            said("agent-1", "coding"),
            started("agent-2", "reviewer"),
            said("agent-2", "reviewing"),
        )
        transcript = app.query_one(Transcript)
        tabs = list(app.query_one(ConversationTabs).query(Tab))

        await pilot.click(tabs[3])
        await pilot.pause()
        assert app.focused is None
        assert transcript.only == "agent-2"
        assert transcript.border_title == "Conversation · reviewer"
        assert shown(app) == ["▶ agent-2 (reviewer) started", "agent-2: reviewing"]
        await feed_events(session, pilot, said("agent-1", "more"))
        assert shown(app) == ["▶ agent-2 (reviewer) started", "agent-2: reviewing"]

        await pilot.click(tabs[0])
        await pilot.pause()
        assert transcript.only is None
        assert transcript.border_title == "Conversation"
        assert len(shown(app)) == 6


async def test_an_exited_agents_tab_goes_and_its_entries_stay_under_all() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session, pilot, started("agent-1"), said("agent-1", "coding"), started("agent-2")
        )
        transcript = app.query_one(Transcript)
        await pilot.click(list(app.query_one(ConversationTabs).query(Tab))[2])
        await pilot.pause()
        assert transcript.only == "agent-1"

        # Another agent leaving doesn't change what's shown.
        await feed_events(session, pilot, AgentExited(agent_id="agent-2", reason="finished"))
        assert tab_strip(app) == ["All", "Planner", "coder"]
        assert transcript.only == "agent-1"

        await feed_events(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        assert tab_strip(app) == ["All", "Planner"]
        assert transcript.only is None
        assert transcript.border_title == "Conversation"
        assert "agent-1: coding" in shown(app)


async def test_the_shown_tabs_title_follows_its_relabelling() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), started("agent-2"))
        transcript = app.query_one(Transcript)
        await pilot.click(list(app.query_one(ConversationTabs).query(Tab))[3])
        await pilot.pause()
        assert transcript.border_title == "Conversation · coder 2"

        await feed_events(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        assert tab_strip(app) == ["All", "Planner", "coder"]
        assert transcript.only == "agent-2"
        assert transcript.border_title == "Conversation · coder"


async def test_f_no_longer_filters() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "coding"))
        await pilot.press("f")
        assert app.query_one(Transcript).only is None
        assert len(shown(app)) == 2


async def test_background_output_marks_a_tab_until_it_is_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), started("agent-2", "reviewer"))
        await feed_events(session, pilot, said("agent-1", "coding"), said("agent-2", "hmm"))
        assert tab_strip(app) == ["All", "Planner", "coder *", "reviewer *"]
        await feed_events(session, pilot, AssistantMessage(turn_id="t", text="chat"))
        assert tab_strip(app) == ["All", "Planner", "coder *", "reviewer *"]

        await pilot.press("tab", "tab")
        await pilot.pause()
        assert tab_strip(app) == ["All", "Planner", "coder", "reviewer *"]
        assert app.query_one(Transcript).border_title == "Conversation · coder"
        # Output for the tab being shown doesn't mark it.
        await feed_events(session, pilot, said("agent-1", "more"))
        assert tab_strip(app) == ["All", "Planner", "coder", "reviewer *"]

        await pilot.press("shift+tab")
        await pilot.pause()
        await feed_events(session, pilot, said("agent-1", "again"))
        assert tab_strip(app) == ["All", "Planner", "coder *", "reviewer *"]


def active_tab(app: OrbitApp) -> str:
    tab = app.query_one(ConversationTabs).active_tab
    assert tab is not None
    return tab.label_text


async def test_tab_and_shift_tab_cycle_the_conversation_tabs_and_wrap() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), started("agent-2", "reviewer"))
        transcript = app.query_one(Transcript)

        await pilot.press("tab")
        assert active_tab(app) == "Planner"
        assert transcript.only == "planner"
        await pilot.press("tab")
        assert active_tab(app) == "coder"
        assert transcript.only == "agent-1"
        await pilot.press("tab")
        assert active_tab(app) == "reviewer"
        await pilot.press("tab")
        assert active_tab(app) == "All"
        assert transcript.only is None
        await pilot.press("shift+tab")
        assert active_tab(app) == "reviewer"
        assert transcript.only == "agent-2"
        await pilot.press("shift+tab", "shift+tab", "shift+tab")
        assert active_tab(app) == "All"
        assert app.focused is None


async def test_tab_with_no_agents_cycles_all_and_planner() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("tab")
        assert active_tab(app) == "Planner"
        await pilot.press("tab")
        assert active_tab(app) == "All"
        await pilot.press("shift+tab", "shift+tab")
        assert active_tab(app) == "All"
        assert app.focused is None
        assert app.query_one(Transcript).only is None


async def test_tab_shows_in_the_footer() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(200, 30)):
        keys = {b.binding.description for b in app.active_bindings.values() if b.binding.show}
        assert "Next agent" in keys


async def test_tab_keeps_its_normal_behaviour_outside_normal_mode() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"))

        # Typing in the prompt.
        await pilot.press("i", "tab")
        assert active_tab(app) == "All"
        assert app.focused is not None
        await pilot.press("escape")
        assert app.focused is None

        # The task list in list mode, then its editor, where j/k still move between fields.
        await pilot.press("l", "k", "enter")
        assert app.focused is app.query_one(TaskList)
        await pilot.press("shift+tab")
        assert active_tab(app) == "All"
        app.set_focus(None)
        await pilot.press("l", "k", "enter", "enter")
        assert len(app.screen_stack) == 2
        await pilot.press("tab", "shift+tab", "j")
        assert active_tab(app) == "All"
        assert app.screen.query_one("#task-model").has_class("selected")
        await pilot.press("i", "tab")
        assert active_tab(app) == "All"


async def test_planner_output_shows_under_planner_and_all_but_not_other_tabs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        # The user's planner prompt is on the Planner tab before the planner starts.
        await pilot.press("i", *"plan", "enter", "tab")
        await feed_events(
            session,
            pilot,
            started("agent-1", "planner"),
            said("agent-1", "drafting"),
            AgentReply(agent_id="agent-1", request_id="r", text="Added."),
            started("agent-2"),
            said("agent-2", "coding"),
        )
        # The planner gets no tab of its own.
        assert tab_strip(app) == ["All", "Planner", "coder *"]
        transcript = app.query_one(Transcript)
        assert transcript.border_title == "Conversation · Planner"
        planner = [
            "plan",
            "▶ agent-1 (planner) started",
            "agent-1: drafting",
            "orbit → agent-1: Added.",
        ]
        assert shown(app) == planner

        await pilot.press("tab")
        assert shown(app) == ["▶ agent-2 (coder) started", "agent-2: coding"]
        # Output while another tab is shown marks the Planner tab.
        await feed_events(session, pilot, said("agent-1", "more"))
        assert tab_strip(app) == ["All", "Planner *", "coder"]

        await pilot.press("tab")
        assert active_tab(app) == "All"
        assert shown(app) == [
            *planner,
            "▶ agent-2 (coder) started",
            "agent-2: coding",
            "agent-1: more",
        ]

        # The planner exiting leaves its tab.
        await feed_events(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        await pilot.press("tab")
        assert tab_strip(app) == ["All", "Planner", "coder"]
        assert shown(app)[-1] == "■ agent-1 exited"


async def test_planner_turn_end_notes_and_errors_show_under_planner_and_all() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1", "planner"), started("agent-2"))
        # Show the coder's tab, so the planner's entries mark the Planner tab.
        await pilot.press("tab", "tab")
        assert active_tab(app) == "coder"
        await feed_events(
            session,
            pilot,
            TurnFinished(turn_id="t", agent_id="agent-1", reason="cancelled"),
            ErrorOccurred(agent_id="agent-1", message="boom"),
        )
        assert tab_strip(app) == ["All", "Planner *", "coder"]
        assert shown(app) == ["▶ agent-2 (coder) started"]

        await pilot.press("tab")
        assert active_tab(app) == "All"
        assert shown(app)[-2:] == ["[turn ended: cancelled]", "boom"]

        await pilot.press("tab")
        assert active_tab(app) == "Planner"
        assert shown(app) == ["▶ agent-1 (planner) started", "[turn ended: cancelled]", "boom"]


async def test_the_prompt_talks_to_the_planner_esc_stops_the_run_and_ctrl_x_the_planner() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        prompt = app.query_one(PromptInput)
        await pilot.press("tab")
        assert prompt.border_title == "Prompt · planner"
        # esc stops the task run, even on the Planner tab; ctrl+x stops the planner.
        await pilot.press("i", *"plan", "enter", "escape", "ctrl+x")
        await pilot.press("shift+tab")
        assert prompt.border_title == "Prompt · planner"
        await feed_events(session, pilot, BusyChanged(busy=False, lane="planner"))
        await pilot.press("i", *"hi", "enter", "escape", "ctrl+x")
        assert session.commands == [
            SubmitPrompt(text="plan"),
            CancelTurn(lane="work"),
            CancelTurn(lane="planner"),
            SubmitPrompt(text="hi"),
            CancelTurn(lane="work"),
            CancelTurn(lane="planner"),
        ]
        await pilot.press("tab")
        assert shown(app) == ["plan", "hi"]


async def test_the_prompt_is_enabled_while_a_run_is_busy() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        prompt = app.query_one(PromptInput)
        await feed_events(session, pilot, BusyChanged(busy=True))
        assert not prompt.disabled
        await pilot.press("i", *"plan", "enter")
        assert prompt.disabled  # Until the planner answers.
        await feed_events(session, pilot, BusyChanged(busy=False, lane="planner"))
        assert not prompt.disabled
        # The work lane going idle doesn't free the prompt while the planner answers.
        await pilot.press("i", *"more", "enter")
        await feed_events(session, pilot, BusyChanged(busy=False, lane="work"))
        assert prompt.disabled


async def test_ctrl_t_jumps_to_the_planner_tab_and_back() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"), started("agent-2", "reviewer"))
        transcript = app.query_one(Transcript)
        await pilot.press("ctrl+t")
        assert active_tab(app) == "Planner"
        assert transcript.border_title == "Conversation · Planner"
        await pilot.press("ctrl+t")
        assert active_tab(app) == "All"

        await pilot.press("tab", "tab", "tab")
        assert active_tab(app) == "reviewer"
        await pilot.press("ctrl+t")
        assert active_tab(app) == "Planner"
        await pilot.press("ctrl+t")
        assert active_tab(app) == "reviewer"

        # Back to All when the tab it came from has gone.
        await pilot.press("ctrl+t")
        await feed_events(session, pilot, AgentExited(agent_id="agent-2", reason="finished"))
        await pilot.press("ctrl+t")
        assert active_tab(app) == "All"
        assert session.commands == []


async def test_ctrl_t_while_typing_keeps_the_prompt_and_its_text() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"))
        prompt = app.query_one(PromptInput)
        await pilot.press("i", *"hello")
        await pilot.press("ctrl+t")
        assert active_tab(app) == "Planner"
        assert app.focused is prompt
        assert prompt.value == "hello"
        await pilot.press("ctrl+t")
        assert active_tab(app) == "All"
        assert app.focused is prompt
        assert prompt.value == "hello"
        assert session.commands == []
