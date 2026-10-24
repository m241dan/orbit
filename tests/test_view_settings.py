import pytest

from orbit.protocol import (
    Settings,
    ToolAccess,
)
from orbit.tui.view.settings import (
    MODEL_TITLE,
    TOOL_ACCESS_TITLE,
    model_choices,
    model_options,
    picked_model,
    picked_tool_access,
    settings_fields,
    settings_summary,
    tool_access_choices,
    tool_access_labels,
)


def test_settings_summary_names_the_model_and_new_tasks_tool_access() -> None:
    assert settings_summary(Settings()) == "planner's model: default · new tasks' tools: restricted"
    full = Settings(model="opus", tool_access="full")
    assert (
        settings_summary(full) == "planner's model: opus · new tasks' tools: full (auto-approved)"
    )


def test_the_settings_are_titled_model_and_tool_access() -> None:
    assert (MODEL_TITLE, TOOL_ACCESS_TITLE) == ("Model", "Tool access")


def test_model_options_offer_only_the_default_without_choices() -> None:
    assert model_options(()) == ((None,), ["default"])


def test_model_options_put_the_default_before_the_choices() -> None:
    assert model_options(("opus", "sonnet")) == (
        (None, "opus", "sonnet"),
        ["default", "opus", "sonnet"],
    )


def test_tool_access_labels_describe_each_option_in_order() -> None:
    assert tool_access_labels() == [
        "off: no tools",
        "restricted: read-only; anything needing approval is refused",
        "full: auto-approve everything",
    ]


def test_model_choices_offer_the_default_then_each_model_and_mark_the_current_one() -> None:
    settings = Settings(model="sonnet", tool_access="full")
    assert model_choices(("opus", "sonnet"), settings) == (
        [
            Settings(model=None, tool_access="full"),
            Settings(model="opus", tool_access="full"),
            Settings(model="sonnet", tool_access="full"),
        ],
        2,
    )


def test_model_choices_mark_the_default_when_no_model_is_set() -> None:
    _, current = model_choices(("opus",), Settings())
    assert current == 0


def test_model_choices_mark_nothing_when_the_model_is_not_offered() -> None:
    options, current = model_choices(("opus",), Settings(model="haiku"))
    assert options == [Settings(), Settings(model="opus")]
    assert current is None


def test_tool_access_choices_offer_each_access_in_order_and_mark_the_current_one() -> None:
    settings = Settings(model="opus", tool_access="off")
    assert tool_access_choices(settings) == (
        [
            Settings(model="opus", tool_access="off"),
            Settings(model="opus", tool_access="restricted"),
            Settings(model="opus", tool_access="full"),
        ],
        0,
    )


def test_picked_model_changes_only_the_model() -> None:
    settings = Settings(model="opus", tool_access="off")
    assert picked_model(("opus", "sonnet"), settings, 2) == Settings(
        model="sonnet", tool_access="off"
    )
    assert picked_model(("opus",), settings, 0) == Settings(tool_access="off")


def test_picked_tool_access_changes_only_the_tool_access() -> None:
    settings = Settings(model="opus", tool_access="off")
    assert picked_tool_access(settings, 2) == Settings(model="opus", tool_access="full")


@pytest.mark.parametrize(
    ("access", "tools"),
    [("off", "off"), ("restricted", "restricted"), ("full", "full (auto-approved)")],
)
def test_settings_fields_show_the_model_and_the_tool_access_label(
    access: ToolAccess, tools: str
) -> None:
    assert settings_fields(Settings(model="opus", tool_access=access)) == ("opus", tools)


def test_settings_fields_call_no_model_the_default() -> None:
    assert settings_fields(Settings())[0] == "default"
