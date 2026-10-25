"""Tests for the settings of the session open, by conversational role."""

from orbit.agent.application.settings import SessionSettings
from orbit.agent.domain.runtimes import CLAUDE, ModelAliases
from orbit.protocol import PLANNER, Settings, SettingsChanged

GIVEN = Settings(model="opus", tool_access="restricted")
SAVED = Settings(model="sonnet", tool_access="full")
ARCHITECTS = Settings(model="haiku", tool_access="off", effort="max")

PLANNERS_MODELS = ModelAliases(CLAUDE, {"opus": "opus", "fast": "claude-haiku-5-5"}, "claude-")
"""The models the planner's runtime takes."""

ARCHITECTS_MODELS = ModelAliases("kiro", {"auto": "kiro-auto"}, "kiro-")
"""The models the architect's runtime takes, another runtime's."""


def models_of(role: str) -> ModelAliases | None:
    """The models ``role``'s runtime takes: the planner's and the architect's differ, and no
    other role's are known."""
    return {PLANNER: PLANNERS_MODELS, "architect": ARCHITECTS_MODELS}.get(role)


def test_the_current_settings_start_as_given() -> None:
    settings = SessionSettings(GIVEN, ("model",), models_of)
    assert (settings.current, settings.given) == (GIVEN, GIVEN)
    assert settings.pinned == ("model",)


def test_the_choices_are_the_aliases_of_the_planners_runtime_with_their_ids() -> None:
    settings = SessionSettings(GIVEN, models=models_of)
    ids = {"opus": "opus", "fast": "claude-haiku-5-5"}
    assert settings.choices() == (("opus", "fast"), ids)


def test_each_roles_choices_are_its_runtimes_aliases() -> None:
    settings = SessionSettings(GIVEN, models=models_of)
    assert settings.choices("architect") == (("auto",), {"auto": "kiro-auto"})


def test_a_role_whose_runtimes_models_arent_known_has_no_choices() -> None:
    assert SessionSettings(GIVEN, models=models_of).choices("helper") == ((), {})
    assert SessionSettings(GIVEN).choices() == ((), {})


def test_the_planners_settings_are_the_current_ones_and_saved_from_the_start() -> None:
    settings = SessionSettings(GIVEN)
    assert settings.of(PLANNER) == settings.current == GIVEN
    assert settings.roles == {PLANNER: GIVEN}


def test_a_role_without_settings_of_its_own_has_the_given_ones() -> None:
    settings = SessionSettings(GIVEN)
    assert settings.of("architect") == GIVEN


def test_resume_without_pins_takes_the_saved_settings() -> None:
    settings = SessionSettings(GIVEN)
    settings.resume({PLANNER: SAVED})
    assert settings.current == SAVED


def test_resume_takes_each_roles_saved_settings() -> None:
    settings = SessionSettings(GIVEN)
    settings.resume({PLANNER: SAVED, "architect": ARCHITECTS})
    assert (settings.of(PLANNER), settings.of("architect")) == (SAVED, ARCHITECTS)
    assert settings.roles == {PLANNER: SAVED, "architect": ARCHITECTS}


def test_resume_without_the_planners_settings_gives_it_the_given_ones() -> None:
    settings = SessionSettings(GIVEN)
    settings.update(SAVED)
    settings.resume({"architect": ARCHITECTS})
    assert settings.roles == {PLANNER: GIVEN, "architect": ARCHITECTS}


def test_resume_keeps_a_pinned_field_as_given() -> None:
    settings = SessionSettings(GIVEN, ("model",))
    settings.resume({PLANNER: SAVED, "architect": ARCHITECTS})
    assert settings.current == Settings(model="opus", tool_access="full")
    assert settings.of("architect") == Settings(model="opus", tool_access="off", effort="max")


def test_resume_with_both_fields_pinned_keeps_the_given_settings() -> None:
    settings = SessionSettings(GIVEN, ("model", "tool_access"))
    settings.resume({PLANNER: SAVED})
    assert settings.current == GIVEN


def test_update_changes_the_current_settings_but_not_the_given() -> None:
    settings = SessionSettings(GIVEN)
    settings.update(SAVED)
    assert (settings.current, settings.given) == (SAVED, GIVEN)


def test_updating_a_roles_settings_leaves_the_planners() -> None:
    settings = SessionSettings(GIVEN)
    settings.update(ARCHITECTS, "architect")
    assert (settings.current, settings.of("architect")) == (GIVEN, ARCHITECTS)


def test_the_settings_saved_are_a_copy() -> None:
    settings = SessionSettings(GIVEN)
    saved = settings.roles
    settings.update(SAVED)
    assert saved == {PLANNER: GIVEN}


def test_changed_carries_the_current_settings_and_the_model_choices_with_their_ids() -> None:
    settings = SessionSettings(GIVEN, models=models_of)
    settings.update(SAVED)
    assert settings.changed() == SettingsChanged(
        settings=SAVED,
        model_choices=("opus", "fast"),
        model_ids={"opus": "opus", "fast": "claude-haiku-5-5"},
    )


def test_changed_for_a_role_carries_its_settings_and_its_runtimes_choices() -> None:
    settings = SessionSettings(GIVEN, models=models_of)
    settings.update(ARCHITECTS, "architect")
    changed = SettingsChanged(
        role="architect",
        settings=ARCHITECTS,
        model_choices=("auto",),
        model_ids={"auto": "kiro-auto"},
    )
    assert settings.changed("architect") == changed
