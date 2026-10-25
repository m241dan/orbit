"""The SettingsPickers showing their model as picker screens; what the pickers decide is checked
in test_pickers."""

from rich.text import Text
from textual.app import App
from textual.widgets import OptionList

from orbit.protocol import AgentEntry, Settings
from orbit.tui.settings_pickers import SettingsPickers
from orbit.tui.widgets import PickerScreen


class PickersApp(App[None]):
    """An app holding installed SettingsPickers, recording the settings picked."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.picked: list[Settings] = []
        self.pickers = SettingsPickers()

    def on_mount(self) -> None:
        self.pickers.install(self)

    def current(self) -> Settings:
        return self.settings

    def pick(self, settings: Settings) -> None:
        self.picked.append(settings)


def shown(app: PickersApp) -> tuple[str, list[str], int | None]:
    """The open picker's title, option labels and highlighted index."""
    screen = app.screen
    assert isinstance(screen, PickerScreen)
    options = screen.query_one(OptionList)
    labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
    return screen.title_text, labels, options.highlighted


async def test_choosing_a_model_offers_the_choices_and_passes_on_the_model_picked() -> None:
    app = PickersApp(Settings(model="opus", tool_access="off"))
    async with app.run_test() as pilot:
        app.pickers.model.show_choices(("opus", "sonnet"))
        app.pickers.model.choose_model(app.current, app.pick)
        await pilot.pause()
        assert shown(app) == ("Model", ["default", "opus", "sonnet"], 1)
        assert app.screen is app.get_screen("pick-model")
        await pilot.press("down", "enter")
        assert app.picked == [Settings(model="sonnet", tool_access="off")]


async def test_the_model_picker_mutes_the_id_each_choice_resolves_to() -> None:
    app = PickersApp(Settings())
    async with app.run_test() as pilot:
        app.pickers.model.show_choices(("opus", "fast"), {"fast": "claude-haiku-5-5"})
        app.pickers.model.choose_model(app.current, app.pick)
        await pilot.pause()
        assert shown(app)[1] == ["default", "opus", "fast  claude-haiku-5-5"]
        options = app.screen.query_one(OptionList)
        prompt = options.get_option_at_index(2).prompt
        assert isinstance(prompt, Text)
        assert [(span.start, span.style) for span in prompt.spans] == [(4, "dim")]
        await pilot.press("end", "enter")
        assert app.picked == [Settings(model="fast")]


async def test_choosing_tools_passes_on_the_tool_access_picked() -> None:
    app = PickersApp(Settings(model="opus", tool_access="off"))
    async with app.run_test() as pilot:
        app.pickers.model.choose_tools(app.current, app.pick)
        await pilot.pause()
        assert shown(app)[0::2] == ("Tool access", 0)
        assert app.screen is app.get_screen("pick-tool-access")
        await pilot.press("end", "enter")
        assert app.picked == [Settings(model="opus", tool_access="full")]


async def test_escape_picks_nothing() -> None:
    app = PickersApp(Settings())
    async with app.run_test() as pilot:
        app.pickers.model.choose_model(app.current, app.pick)
        await pilot.pause()
        await pilot.press("escape")
        assert app.picked == []


async def test_new_model_choices_relabel_the_model_picker_and_apply_to_picks() -> None:
    app = PickersApp(Settings())
    async with app.run_test() as pilot:
        app.pickers.model.show_choices(("opus",))
        app.pickers.model.choose_model(app.current, app.pick)
        await pilot.pause()
        app.pickers.model.show_choices(("haiku", "fable"))
        await pilot.pause()
        assert shown(app)[1] == ["default", "haiku", "fable"]
        await pilot.press("end", "enter")
        assert app.picked == [Settings(model="fable")]


async def test_choosing_an_effort_passes_on_the_effort_picked() -> None:
    app = PickersApp(Settings(model="opus", effort="high"))
    async with app.run_test() as pilot:
        app.pickers.model.choose_effort(app.current, app.pick)
        await pilot.pause()
        assert shown(app) == ("Effort", ["unset", "low", "medium", "high", "xhigh", "max"], 3)
        assert app.screen is app.get_screen("pick-effort")
        await pilot.press("end", "enter")
        assert app.picked == [Settings(model="opus", effort="max")]


async def test_choosing_a_role_offers_the_roles_given_and_passes_on_the_role_picked() -> None:
    coder = AgentEntry(name="coder", label="Coder", conversational=False)
    security = AgentEntry(name="security", label="security", conversational=False, lane="audit")
    app = PickersApp(Settings())
    picked: list[AgentEntry] = []
    async with app.run_test() as pilot:
        app.pickers.model.choose_role((coder,), "coder", picked.append)
        await pilot.pause()
        assert shown(app) == ("Role", ["Coder · work"], 0)
        await pilot.press("escape")
        app.pickers.model.choose_role((coder, security), "coder", picked.append)
        await pilot.pause()
        assert shown(app) == ("Role", ["Coder · work", "security · audit"], 0)
        assert app.screen is app.get_screen("pick-role")
        await pilot.press("down", "enter")
        assert picked == [security]
