from orbit.tui.view import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    RESET_MESSAGE,
    RESET_TITLE,
    RESET_YES,
    confirm_hint,
)


def test_confirm_hint_names_what_y_does() -> None:
    assert confirm_hint("reset") == "y reset · n/esc cancel"


def test_configure_confirmation_asks_to_set_up_checks() -> None:
    assert CONFIGURE_TITLE == "Set up checks?"
    assert CONFIGURE_MESSAGE == (
        "A configurator task will go to the front of the task list and run now."
    )
    assert CONFIGURE_YES == "set up"


def test_reset_confirmation_asks_to_reset_the_planner() -> None:
    assert RESET_TITLE == "Reset the planner?"
    assert RESET_MESSAGE == (
        "The planner's conversation ends, and the next prompt starts a fresh one with its role"
        " read again from .orbit/config.toml. Draft tasks stay."
    )
    assert RESET_YES == "reset"
