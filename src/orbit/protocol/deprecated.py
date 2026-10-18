"""The messages still in the protocol that are deprecated, to be removed at its next major
version."""

from orbit.protocol.commands import ListSessions, OpenSession
from orbit.protocol.events import SessionsListed
from orbit.protocol.models import _Message

DEPRECATED_MESSAGES: tuple[type[_Message], ...] = (OpenSession, ListSessions, SessionsListed)
"""The deprecated commands and events, removed from ``Command`` and ``Event`` at the next major
``PROTOCOL_VERSION``."""
