"""Tests for the settings of the session open, by conversational role."""

from orbit.agent.application.settings import SessionSettings
from orbit.protocol import PLANNER, Settings, SettingsChanged

GIVEN = Settings(model="opus", tool_access="restricted")
SAVED = Settings(model="sonnet", tool_access="full")
ARCHITECTS = Settings(model="haiku", tool_access="off", effort="max")


def test_the_current_settings_start_as_given() -> None:
    settings = SessionSettings(GIVEN, ("model",), ("opus", "sonnet"))
    assert (settings.current, settings.given) == (GIVEN, GIVEN)
    assert (settings.pinned, settings.model_choices) == (("model",), ("opus", "sonnet"))


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


def test_changed_carries_the_current_settings_and_the_model_choices() -> None:
    settings = SessionSettings(GIVEN, model_choices=("opus", "sonnet"))
    settings.update(SAVED)
    assert settings.changed() == SettingsChanged(settings=SAVED, model_choices=("opus", "sonnet"))


def test_changed_for_a_role_carries_its_settings() -> None:
    settings = SessionSettings(GIVEN, model_choices=("opus",))
    settings.update(ARCHITECTS, "architect")
    changed = SettingsChanged(role="architect", settings=ARCHITECTS, model_choices=("opus",))
    assert settings.changed("architect") == changed
