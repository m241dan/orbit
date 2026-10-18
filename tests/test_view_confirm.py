from orbit.tui.view.confirm import (
    CONFIGURE_MESSAGE,
    CONFIGURE_TITLE,
    CONFIGURE_YES,
    RESET_MESSAGE,
    RESET_TITLE,
    RESET_YES,
    TAKE_OVER_NO,
    TAKE_OVER_TITLE,
    TAKE_OVER_YES,
    confirm_hint,
)


def test_confirm_hint_names_what_y_does() -> None:
    assert confirm_hint("reset") == "y reset · n/esc cancel"


def test_confirm_hint_names_what_n_does_when_it_isnt_cancelling() -> None:
    assert confirm_hint("take over", "stay read-only") == "y take over · n/esc stay read-only"


def test_the_take_over_offer_asks_to_take_over_or_stay_read_only() -> None:
    assert TAKE_OVER_TITLE == "The repository is free"
    assert (TAKE_OVER_YES, TAKE_OVER_NO) == ("take over", "stay read-only")


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
