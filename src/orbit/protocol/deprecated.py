"""The messages still in the protocol that are deprecated, to be removed at its next major
version."""

from orbit.protocol.models import _Message

DEPRECATED_MESSAGES: tuple[type[_Message], ...] = ()
"""The deprecated commands and events, removed from ``Command`` and ``Event`` at the next major
``PROTOCOL_VERSION``; none since version 2 removed those of version 1."""
