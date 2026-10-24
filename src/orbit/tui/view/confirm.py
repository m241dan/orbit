"""The confirmations asked before a key's action."""

from dataclasses import dataclass

CONFIGURE_TITLE = "Set up checks?"
CONFIGURE_MESSAGE = "A configurator task will go to the front of the task list and run now."
CONFIGURE_YES = "set up"

RESET_TITLE = "Reset the planner?"
RESET_MESSAGE = (
    "The planner's conversation ends, and the next prompt starts a fresh one with its role"
    " read again from .orbit/config.toml. Draft tasks stay."
)
RESET_YES = "reset"

TAKE_OVER_TITLE = "The repository is free"
TAKE_OVER_MESSAGE = (
    "The orbit that owned this repository has gone. Take over to run the work queued here and"
    " what you run from now on; stay read-only to leave it for another orbit."
)
TAKE_OVER_YES = "take over"
TAKE_OVER_NO = "stay read-only"


def confirm_hint(yes: str, no: str = "cancel") -> str:
    """The keys a confirmation shows, with ``yes`` naming what y does and ``no`` what n and
    escape do."""
    return f"y {yes} · n/esc {no}"


@dataclass(frozen=True)
class Question:
    """``title``, asked with ``message``, offering ``choices``, of which escape picks the one at
    ``escape``."""

    title: str
    message: str
    choices: tuple[str, ...]
    escape: int
