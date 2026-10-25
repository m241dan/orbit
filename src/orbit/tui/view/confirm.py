"""The confirmations asked before a key's action."""

from dataclasses import dataclass

from orbit.protocol import PLANNER

CONFIGURE_TITLE = "Set up checks?"
CONFIGURE_MESSAGE = "A configurator task will go to the front of the task list and run now."
CONFIGURE_YES = "set up"

RESET_YES = "reset"

TAKE_OVER_TITLE = "The repository is free"
TAKE_OVER_MESSAGE = (
    "The orbit that owned this repository has gone. Take over to run the work queued here and"
    " what you run from now on; stay read-only to leave it for another orbit."
)
TAKE_OVER_YES = "take over"
TAKE_OVER_NO = "stay read-only"


def reset_title(role: str) -> str:
    """The title asking before resetting the conversational ``role``: ``Reset the planner?``."""
    return f"Reset the {role}?"


def reset_message(role: str) -> str:
    """What resetting the conversational ``role`` does, as the confirmation says: only the
    planner's role is read again."""
    reread = " with its role read again from .orbit/config.toml" if role == PLANNER else ""
    return (
        f"The {role}'s conversation ends, and the next prompt starts a fresh one{reread}."
        " Draft tasks stay."
    )


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
