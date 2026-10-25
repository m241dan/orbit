"""The model and tool access pickers, without Textual: what each opens on, the model choices
they offer, and the settings a pick passes on."""

from orbit.protocol import AgentEntry, Settings
from orbit.tui.view.settings import Pickers
from tests.view_fakes import PickerScreens

CODER = AgentEntry(name="coder", label="Coder", conversational=False, tool_access="full")
SECURITY = AgentEntry(name="security", label="security", conversational=False, lane="security")
HELPER = AgentEntry(
    name="helper", label="helper", conversational=False, lane="helper", personal=True
)


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


async def test_choosing_an_effort_opens_on_the_effort_and_passes_on_the_one_picked() -> None:
    screens = Screens()
    picked: list[Settings] = []
    screens.pickers.choose_effort(lambda: Settings(model="opus", effort="low"), picked.append)
    assert [opened[:2] for opened in screens.opened] == [("effort", 1)]
    await screens.pick(5)
    assert picked == [Settings(model="opus", effort="max")]


async def test_an_effort_pick_applies_to_the_settings_current_when_picked() -> None:
    screens = Screens()
    picked: list[Settings] = []
    settings = Settings(effort="high")
    screens.pickers.choose_effort(lambda: settings, picked.append)
    settings = Settings(model="opus", effort="high")
    await screens.pick(0)
    assert picked == [Settings(model="opus")]


async def test_choosing_a_role_labels_the_roles_opens_on_the_role_and_passes_on_the_pick() -> None:
    screens = Screens()
    picked: list[AgentEntry] = []
    screens.pickers.choose_role((CODER, SECURITY), "security", picked.append)
    assert [opened[:2] for opened in screens.opened] == [("role", 1)]
    assert screens.roles == [["Coder · work", "security · security"]]
    await screens.pick(0)
    assert picked == [CODER]


def test_the_role_picker_marks_a_personal_role() -> None:
    screens = Screens()
    screens.pickers.choose_role((CODER, HELPER), "coder", lambda role: None)
    assert screens.roles == [["Coder · work", "helper · helper · personal"]]


def test_the_role_picker_opens_on_nothing_for_a_role_not_offered() -> None:
    screens = Screens()
    screens.pickers.choose_role((CODER,), "writer", lambda role: None)
    assert [opened[:2] for opened in screens.opened] == [("role", None)]
