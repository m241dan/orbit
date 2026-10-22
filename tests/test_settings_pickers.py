"""The SettingsPickers choosing a model and tool access for whoever opens them."""

from textual.app import App
from textual.widgets import OptionList

from orbit.protocol import Settings
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
        app.pickers.show_choices(("opus", "sonnet"))
        app.pickers.choose_model(app.current, app.pick)
        await pilot.pause()
        assert shown(app) == ("Model", ["default", "opus", "sonnet"], 1)
        assert app.screen is app.get_screen("pick-model")
        await pilot.press("down", "enter")
        assert app.picked == [Settings(model="sonnet", tool_access="off")]


async def test_choosing_tools_passes_on_the_tool_access_picked() -> None:
    app = PickersApp(Settings(model="opus", tool_access="off"))
    async with app.run_test() as pilot:
        app.pickers.choose_tools(app.current, app.pick)
        await pilot.pause()
        assert shown(app)[0::2] == ("Tool access", 0)
        assert app.screen is app.get_screen("pick-tool-access")
        await pilot.press("end", "enter")
        assert app.picked == [Settings(model="opus", tool_access="full")]


async def test_a_pick_applies_to_the_settings_current_when_picked() -> None:
    app = PickersApp(Settings(model="opus", tool_access="off"))
    async with app.run_test() as pilot:
        app.pickers.show_choices(("opus", "sonnet"))
        app.pickers.choose_model(app.current, app.pick)
        await pilot.pause()
        app.settings = Settings(model="opus", tool_access="full")
        await pilot.press("down", "enter")
        app.pickers.choose_tools(app.current, app.pick)
        await pilot.pause()
        app.settings = Settings(model="haiku", tool_access="full")
        await pilot.press("home", "enter")
        assert app.picked == [
            Settings(model="sonnet", tool_access="full"),
            Settings(model="haiku", tool_access="off"),
        ]


async def test_escape_picks_nothing() -> None:
    app = PickersApp(Settings())
    async with app.run_test() as pilot:
        app.pickers.choose_model(app.current, app.pick)
        await pilot.pause()
        await pilot.press("escape")
        assert app.picked == []


async def test_new_model_choices_relabel_the_model_picker_and_apply_to_picks() -> None:
    app = PickersApp(Settings())
    async with app.run_test() as pilot:
        app.pickers.show_choices(("opus",))
        app.pickers.choose_model(app.current, app.pick)
        await pilot.pause()
        app.pickers.show_choices(("haiku", "fable"))
        await pilot.pause()
        assert shown(app)[1] == ["default", "haiku", "fable"]
        await pilot.press("end", "enter")
        assert app.picked == [Settings(model="fable")]


async def test_the_same_model_choices_keep_the_model_picker_as_it_is() -> None:
    app = PickersApp(Settings(model="opus"))
    async with app.run_test() as pilot:
        app.pickers.show_choices(("opus",))
        app.pickers.choose_model(app.current, app.pick)
        await pilot.pause()
        app.pickers.show_choices(("opus",))
        await pilot.pause()
        # Relabelling would clear the highlight.
        assert shown(app) == ("Model", ["default", "opus"], 1)
