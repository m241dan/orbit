"""The model and tool access pickers, without Textual: what each opens on, the model choices
they offer, and the settings a pick passes on."""

from orbit.protocol import Settings
from orbit.tui.view.settings import Pickers
from tests.view_fakes import PickerScreens


class Screens(PickerScreens):
    """Picker screens showing their model."""

    def __init__(self) -> None:
        super().__init__()
        self.pickers = Pickers(self)


async def test_choosing_a_model_opens_on_the_model_and_passes_on_the_one_picked() -> None:
    screens = Screens()
    picked: list[Settings] = []
    screens.pickers.show_choices(("opus", "sonnet"))
    assert screens.labels == [["default", "opus", "sonnet"]]
    screens.pickers.choose_model(lambda: Settings(model="opus", tool_access="off"), picked.append)
    assert [opened[:2] for opened in screens.opened] == [("model", 1)]
    await screens.pick(2)
    assert picked == [Settings(model="sonnet", tool_access="off")]


async def test_choosing_tools_opens_on_the_tool_access_and_passes_on_the_one_picked() -> None:
    screens = Screens()
    picked: list[Settings] = []

    async def pick(settings: Settings) -> None:
        picked.append(settings)

    screens.pickers.choose_tools(lambda: Settings(model="opus", tool_access="restricted"), pick)
    assert [opened[:2] for opened in screens.opened] == [("tools", 1)]
    await screens.pick(2)
    assert picked == [Settings(model="opus", tool_access="full")]


async def test_a_pick_applies_to_the_settings_and_choices_current_when_picked() -> None:
    screens = Screens()
    picked: list[Settings] = []
    settings = Settings(model="opus", tool_access="off")
    screens.pickers.show_choices(("opus", "sonnet"))
    screens.pickers.choose_model(lambda: settings, picked.append)
    settings = Settings(model="opus", tool_access="full")
    screens.pickers.show_choices(("haiku", "fable"))
    await screens.pick(2)
    screens.pickers.choose_tools(lambda: settings, picked.append)
    settings = Settings(model="haiku", tool_access="full")
    await screens.pick(0)
    assert picked == [
        Settings(model="fable", tool_access="full"),
        Settings(model="haiku", tool_access="off"),
    ]


def test_the_model_picker_is_relabelled_only_when_the_choices_change() -> None:
    screens = Screens()
    screens.pickers.show_choices(())
    screens.pickers.show_choices(("opus",))
    screens.pickers.show_choices(("opus",))
    assert screens.labels == [["default", "opus"]]
    assert screens.pickers.model_choices == ("opus",)
