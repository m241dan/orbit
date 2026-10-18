"""The confirmations asked before a key's action."""

CONFIGURE_TITLE = "Set up checks?"
CONFIGURE_MESSAGE = "A configurator task will go to the front of the task list and run now."
CONFIGURE_YES = "set up"

RESET_TITLE = "Reset the planner?"
RESET_MESSAGE = (
    "The planner's conversation ends, and the next prompt starts a fresh one with its role"
    " read again from .orbit/config.toml. Draft tasks stay."
)
RESET_YES = "reset"


def confirm_hint(yes: str) -> str:
    """The keys a confirmation shows, with ``yes`` naming what y does."""
    return f"y {yes} · n/esc cancel"
