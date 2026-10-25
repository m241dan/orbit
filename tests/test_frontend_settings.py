"""The settings, without Textual: how they show under the prompt, and what the model and tool
access pickers open on and send, as the settings and model choices change under them."""

from rich.text import Text

from orbit.protocol import Settings, SettingsChanged, UpdateSettings
from tests.frontend_fakes import Shell
from tests.tui_fakes import SNAPSHOT

CHOICES = ("opus", "sonnet")


async def shown(shell: Shell, settings: Settings, choices: tuple[str, ...] = CHOICES) -> None:
    """Have the backend report ``settings``, offering ``choices``."""
    await shell.feed(SettingsChanged(settings=settings, model_choices=choices))


def opened(shell: Shell) -> list[tuple[str, int | None]]:
    """The pickers opened, in turn, and the option each opened on."""
    return [(picker, current) for picker, current, _ in shell.pickers.opened]


async def test_the_settings_show_under_the_prompt_as_reported() -> None:
    shell = Shell()
    assert shell.marks.subtitle == ""
    await shown(shell, Settings())
    assert shell.marks.subtitle == "planner's model: default · new tasks' tools: restricted"
    await shown(shell, Settings(model="opus", tool_access="full"))
    assert shell.marks.subtitle == "planner's model: opus · new tasks' tools: full (auto-approved)"
    await shown(shell, Settings(tool_access="off"))
    assert shell.marks.subtitle == "planner's model: default · new tasks' tools: off"


async def test_another_roles_settings_leave_the_planners_shown() -> None:
    shell = Shell()
    await shown(shell, Settings(model="opus"))
    await shell.feed(SettingsChanged(role="architect", settings=Settings(model="haiku")))
    assert shell.marks.subtitle == "planner's model: opus · new tasks' tools: restricted"


async def test_the_pickers_open_only_once_the_backend_has_reported_settings() -> None:
    shell = Shell()
    shell.frontend.pick_model()
    shell.frontend.pick_tool_access()
    assert opened(shell) == []
    assert shell.wire.commands == []


async def test_a_pick_is_shown_only_once_the_backend_confirms_it() -> None:
    shell = Shell()
    await shown(shell, Settings())
    shell.frontend.pick_tool_access()
    await shell.pickers.pick(2)
    assert shell.marks.subtitle == "planner's model: default · new tasks' tools: restricted"
    assert shell.wire.commands == [UpdateSettings(settings=Settings(tool_access="full"))]
    await shown(shell, Settings(tool_access="full"))
    assert (
        shell.marks.subtitle == "planner's model: default · new tasks' tools: full (auto-approved)"
    )


async def test_the_model_picker_offers_the_choices_and_opens_on_the_model() -> None:
    shell = Shell()
    await shown(shell, Settings(model="opus"))
    shell.frontend.pick_model()
    assert shell.pickers.labels == [["default", "opus", "sonnet"]]
    assert opened(shell) == [("model", 1)]


FAST = Text.assemble("fast", ("  claude-haiku-5-5", "dim"))
"""How the model picker shows the alias ``fast``, resolving to ``claude-haiku-5-5``."""


async def test_the_model_picker_shows_each_choice_with_the_id_settings_say_it_resolves_to() -> None:
    shell = Shell()
    ids = {"opus": "opus", "fast": "claude-haiku-5-5"}
    await shell.feed(
        SettingsChanged(settings=Settings(), model_choices=("opus", "fast"), model_ids=ids)
    )
    assert shell.pickers.labels == [["default", "opus", FAST]]


async def test_the_model_picker_shows_each_choice_with_the_id_a_snapshot_says() -> None:
    shell = Shell()
    ids = {"opus": "opus", "fast": "claude-haiku-5-5"}
    await shell.feed(SNAPSHOT.model_copy(update={"model_choices": ("fast",), "model_ids": ids}))
    assert shell.pickers.labels == [["default", FAST]]


async def test_picking_a_model_requests_it_keeping_the_tool_access() -> None:
    shell = Shell()
    await shown(shell, Settings(model="opus", tool_access="full"))
    shell.frontend.pick_model()
    await shell.pickers.pick(2)
    assert shell.wire.commands == [
        UpdateSettings(settings=Settings(model="sonnet", tool_access="full"))
    ]


async def test_a_model_pick_keeps_the_tool_access_changed_while_the_picker_is_open() -> None:
    shell = Shell()
    await shown(shell, Settings(model="opus", tool_access="full"))
    shell.frontend.pick_model()
    await shown(shell, Settings(model="opus", tool_access="off"))
    await shell.pickers.pick(2)
    assert shell.wire.commands == [
        UpdateSettings(settings=Settings(model="sonnet", tool_access="off"))
    ]


async def test_a_tool_access_pick_keeps_the_model_changed_while_the_picker_is_open() -> None:
    shell = Shell()
    await shown(shell, Settings(model="opus", tool_access="off"))
    shell.frontend.pick_tool_access()
    await shown(shell, Settings(model="sonnet", tool_access="off"))
    await shell.pickers.pick(1)
    assert shell.wire.commands == [
        UpdateSettings(settings=Settings(model="sonnet", tool_access="restricted"))
    ]


async def test_a_model_pick_sends_the_model_labelled_after_the_choices_change() -> None:
    shell = Shell()
    await shown(shell, Settings())
    shell.frontend.pick_model()
    await shown(shell, Settings(), ("haiku", "opus", "sonnet", "fable"))
    assert shell.pickers.labels[-1] == ["default", "haiku", "opus", "sonnet", "fable"]
    await shell.pickers.pick(4)
    assert shell.wire.commands == [UpdateSettings(settings=Settings(model="fable"))]


async def test_the_model_picker_opens_on_nothing_for_a_custom_model() -> None:
    shell = Shell()
    await shown(shell, Settings(model="claude-custom-1"))
    shell.frontend.pick_model()
    assert opened(shell) == [("model", None)]


async def test_the_tool_access_picker_opens_on_the_tool_access_and_picks() -> None:
    shell = Shell()
    await shown(shell, Settings(model="opus", tool_access="off"))
    shell.frontend.pick_tool_access()
    assert opened(shell) == [("tools", 0)]
    await shell.pickers.pick(2)
    assert shell.wire.commands == [
        UpdateSettings(settings=Settings(model="opus", tool_access="full"))
    ]


async def test_the_same_choices_again_leave_the_model_picker_as_it_is() -> None:
    shell = Shell()
    await shown(shell, Settings())
    await shown(shell, Settings(model="opus"))
    await shown(shell, Settings(), ("haiku",))
    assert shell.pickers.labels == [["default", "opus", "sonnet"], ["default", "haiku"]]
