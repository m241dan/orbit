"""Tests for the settings of the session open."""

from orbit.agent.application.settings import SessionSettings
from orbit.protocol import Settings, SettingsChanged

GIVEN = Settings(model="opus", tool_access="restricted")
SAVED = Settings(model="sonnet", tool_access="full")


def test_the_current_settings_start_as_given() -> None:
    settings = SessionSettings(GIVEN, ("model",), ("opus", "sonnet"))
    assert (settings.current, settings.given) == (GIVEN, GIVEN)
    assert (settings.pinned, settings.model_choices) == (("model",), ("opus", "sonnet"))


def test_resume_without_pins_takes_the_saved_settings() -> None:
    settings = SessionSettings(GIVEN)
    settings.resume(SAVED)
    assert settings.current == SAVED


def test_resume_keeps_a_pinned_field_as_given() -> None:
    settings = SessionSettings(GIVEN, ("model",))
    settings.resume(SAVED)
    assert settings.current == Settings(model="opus", tool_access="full")


def test_resume_with_both_fields_pinned_keeps_the_given_settings() -> None:
    settings = SessionSettings(GIVEN, ("model", "tool_access"))
    settings.resume(SAVED)
    assert settings.current == GIVEN


def test_update_changes_the_current_settings_but_not_the_given() -> None:
    settings = SessionSettings(GIVEN)
    settings.update(SAVED)
    assert (settings.current, settings.given) == (SAVED, GIVEN)


def test_changed_carries_the_current_settings_and_the_model_choices() -> None:
    settings = SessionSettings(GIVEN, model_choices=("opus", "sonnet"))
    settings.update(SAVED)
    assert settings.changed() == SettingsChanged(settings=SAVED, model_choices=("opus", "sonnet"))
