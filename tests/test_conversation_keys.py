"""ctrl+t, ctrl+y, ctrl+x, ctrl+n and the prompt in the app: the keys acting on the
conversational role whose tab is shown, and how the footer labels them."""

from textual.widgets._footer import FooterKey

from orbit.protocol import (
    PLANNER,
    AgentEntry,
    Command,
    SendMessage,
    Settings,
    SettingsChanged,
    UpdateSettings,
)
from orbit.tui import OrbitApp
from orbit.tui.widgets import ChatPrompt, PickerScreen
from tests.session_apps import with_agents
from tests.tui_fakes import FakeSession, active_tab, feed_events, picker, show_settings

TALKING = with_agents(
    AgentEntry(name=PLANNER, label="Planner", conversational=True, lane="planner"),
    AgentEntry(name="architect", label="Architect", conversational=True, lane="design"),
)
"""A catalog listing the architect, a project's own conversational role, beside the planner."""


def talked(session: FakeSession) -> list[Command]:
    """The messages and settings ``session`` got."""
    return [c for c in session.commands if isinstance(c, SendMessage | UpdateSettings)]


async def test_the_footer_labels_ctrl_t_conversations_and_ctrl_y_effort() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(300, 30)) as pilot:
        await pilot.pause()
        labels = [key.description for key in app.query(FooterKey)]
        assert "Conversations" in labels
        assert "Effort" in labels


async def test_the_footer_labels_ctrl_x_and_ctrl_n_without_naming_the_planner() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(300, 30)) as pilot:
        await pilot.pause()
        labels = [key.description for key in app.query(FooterKey)]
        assert "Stop turn" in labels
        assert "Reset conversation" in labels
        assert not [label for label in labels if "planner" in label.lower()]


async def test_while_typing_ctrl_y_redoes_in_the_prompt_rather_than_picking_effort() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus"))
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i", *"draft", "ctrl+z")
        assert prompt.text == ""
        await pilot.press("ctrl+y")
        assert prompt.text == "draft"
        assert not isinstance(app.screen, PickerScreen)
        await pilot.press("escape", "ctrl+y")
        assert picker(app)[0] == "Effort"
        assert talked(session) == []


async def test_ctrl_y_picks_the_planners_effort() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus"))
        await pilot.press("ctrl+y")
        assert picker(app)[0::2] == ("Effort", 0)
        await pilot.press("j", "j", "j", "enter")
        assert talked(session) == [UpdateSettings(settings=Settings(model="opus", effort="high"))]


async def test_on_the_architects_tab_the_prompt_and_ctrl_y_talk_to_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, TALKING, SettingsChanged(settings=Settings(model="opus")))
        await pilot.press("ctrl+t", "ctrl+t")
        assert active_tab(app) == "architect"
        prompt = app.query_one(ChatPrompt)
        assert prompt.placeholder == "Press i to talk to Architect, esc to stop typing"
        assert prompt.border_title == "Prompt · architect"
        await pilot.press("ctrl+y", "end", "enter")
        await pilot.press("i", *"design", "enter")
        await pilot.pause()
        assert talked(session) == [
            UpdateSettings(role="architect", settings=Settings(model="opus", effort="max")),
            SendMessage(agent="architect", text="design"),
        ]
        await pilot.press("escape", "ctrl+t")
        assert active_tab(app) == "All"
        assert prompt.placeholder == "Press i to talk to Planner, esc to stop typing"
