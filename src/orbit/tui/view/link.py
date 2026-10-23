"""How the link to a control plane in another process is shown."""

from orbit.protocol import Ended, HandshakeFailed, TokenRefused

LOST_AFTER = 120.0
"""How many seconds of reconnecting go by before the user is asked whether to keep trying."""

KEEP_TRYING = "Keep trying"
QUIT = "Quit"
CHOICES = (KEEP_TRYING, QUIT)
"""The choices a lost or stopped control plane offers, the first preselected."""

NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)

STOPPED_TITLE = "The control plane stopped"
REFUSED_TITLE = "Can't connect"
ENDED_TITLE = "Disconnected"


def reconnecting_status(url: str) -> str:
    """The status line shown by the prompt while reconnecting to ``url``."""
    return f"Reconnecting to {url}…"


def lost_title(url: str) -> str:
    """The title of the modal asking whether to keep trying ``url``."""
    return f"Lost the control plane at {url}"


def lost_message(url: str) -> str:
    """What the modal asking whether to keep trying ``url`` says."""
    return f"Orbit has been trying to reconnect to {url} for {minutes_text(LOST_AFTER)}."


def minutes_text(seconds: float) -> str:
    """The whole minutes in ``seconds`` in words: ``one minute``, ``two minutes``, or ``12
    minutes`` past ten."""
    minutes = int(seconds // 60)
    count = NUMBER_WORDS[minutes] if minutes < len(NUMBER_WORDS) else str(minutes)
    return f"{count} minute{'s' * (minutes != 1)}"


def stopped_message(url: str) -> str:
    """What the modal saying the control plane at ``url`` stopped says."""
    return f"The control plane at {url} is shutting down. Orbit keeps trying to reconnect."


def ended_message(url: str, reason: str) -> str:
    """What the modal saying the connection to the control plane at ``url`` ended for good, for
    ``reason`` if one was given, says."""
    ended = f"The connection to the control plane at {url} ended"
    because = f": {reason}." if reason else "."
    return f"{ended}{because} Reconnecting would end the same way."


def final_notice(state: TokenRefused | HandshakeFailed | Ended) -> tuple[str, str]:
    """The title and message of the modal saying why the link, in ``state``, isn't retried."""
    if isinstance(state, TokenRefused | HandshakeFailed):
        return REFUSED_TITLE, state.message
    return ENDED_TITLE, ended_message(state.url, state.reason)


def lost_delay(since: float, now: float) -> float:
    """How many seconds from ``now`` until the user is asked whether to keep trying, for a link
    down ``since``."""
    return max(0.0, LOST_AFTER - (now - since))
