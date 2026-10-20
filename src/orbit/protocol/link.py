"""How a frontend's link to a control plane in another process stands, as the session it talks
through reports it."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

NOT_CONNECTED = "Not connected"
"""Why a command sent while the link is down wasn't sent."""


@dataclass(frozen=True)
class Connected:
    """The link to the control plane at ``url`` is up."""

    url: str


@dataclass(frozen=True)
class Reconnecting:
    """The link to the control plane at ``url`` is down and being retried, since ``since``, a
    ``time.monotonic()`` reading."""

    url: str
    since: float


@dataclass(frozen=True)
class Stopped:
    """The control plane at ``url`` said it was shutting down; the link is retried after it."""

    url: str


@dataclass(frozen=True)
class TokenRefused:
    """The control plane turned the token away, as ``message`` says; the link isn't retried."""

    message: str


@dataclass(frozen=True)
class Ended:
    """The connection to the control plane at ``url`` ended in a way reconnecting would only
    repeat, for ``reason``, which may be empty; the link isn't retried."""

    url: str
    reason: str


@dataclass(frozen=True)
class HandshakeFailed:
    """The connection to the control plane at ``url`` can't be made safely as asked, such as
    for a certificate that isn't trusted, as ``message`` says; the link isn't retried."""

    url: str
    message: str


LinkState = Connected | Reconnecting | Stopped | TokenRefused | Ended | HandshakeFailed


class NotConnected(Exception):
    """A command couldn't be sent because the link to the control plane is down."""

    def __init__(self) -> None:
        super().__init__(NOT_CONNECTED)


class Link(Protocol):
    """A session's link to a control plane in another process, which reports how it stands."""

    def watch(self, changed: Callable[[LinkState], None]) -> None:
        """Have ``changed`` called with each state the link moves to."""
        ...
