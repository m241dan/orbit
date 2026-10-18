"""How the link to a control plane in another process is shown."""

LOST_AFTER = 120.0
"""How many seconds of reconnecting go by before the user is asked whether to keep trying."""

KEEP_TRYING = "Keep trying"
QUIT = "Quit"
CHOICES = (KEEP_TRYING, QUIT)
"""The choices a lost or stopped control plane offers, the first preselected."""

STOPPED_TITLE = "The control plane stopped"
REFUSED_TITLE = "Can't connect"


def reconnecting_status(url: str) -> str:
    """The status line shown by the prompt while reconnecting to ``url``."""
    return f"Reconnecting to {url}…"


def lost_title(url: str) -> str:
    """The title of the modal asking whether to keep trying ``url``."""
    return f"Lost the control plane at {url}"


def lost_message(url: str) -> str:
    """What the modal asking whether to keep trying ``url`` says."""
    return f"Orbit has been trying to reconnect to {url} for two minutes."


def stopped_message(url: str) -> str:
    """What the modal saying the control plane at ``url`` stopped says."""
    return f"The control plane at {url} is shutting down. Orbit keeps trying to reconnect."


def lost_delay(since: float, now: float) -> float:
    """How many seconds from ``now`` until the user is asked whether to keep trying, for a link
    down ``since``."""
    return max(0.0, LOST_AFTER - (now - since))
