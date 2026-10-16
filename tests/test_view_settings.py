from orbit.protocol import (
    Settings,
)
from orbit.tui.view import (
    model_options,
    settings_summary,
    tool_access_labels,
)


def test_settings_summary_names_the_model_and_new_tasks_tool_access() -> None:
    assert settings_summary(Settings()) == "planner's model: default · new tasks' tools: restricted"
    full = Settings(model="opus", tool_access="full")
    assert (
        settings_summary(full) == "planner's model: opus · new tasks' tools: full (auto-approved)"
    )


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
