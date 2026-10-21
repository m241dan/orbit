"""The deprecated messages: listed, documented, and gone by the next major protocol version."""

from pathlib import Path
from typing import Any, get_args

import orbit.protocol
from orbit.protocol import DEPRECATED_MESSAGES, PROTOCOL_VERSION, Command, Event

PROTOCOL_DOC = Path(__file__).parent.parent / "docs" / "protocol.md"

DEPRECATED_IN_VERSION = 2
"""The protocol version the messages in ``DEPRECATED_MESSAGES`` were deprecated in."""


def members(union: Any) -> set[type]:
    """The message classes in the discriminated ``union``."""
    return set(get_args(get_args(union)[0]))


def test_nothing_is_deprecated_in_protocol_version_2() -> None:
    assert DEPRECATED_MESSAGES == ()


def test_version_2_removed_the_old_session_commands_and_listing() -> None:
    present = {message.__name__ for message in members(Command) | members(Event)}
    for name in ("OpenSession", "ListSessions", "SessionsListed", "DEFAULT_WORKSPACE"):
        assert name not in present and not hasattr(orbit.protocol, name)


def test_deprecated_messages_are_removed_past_the_version_they_were_deprecated_in() -> None:
    present = members(Command) | members(Event)
    lingering = [message.__name__ for message in DEPRECATED_MESSAGES if message in present]
    assert PROTOCOL_VERSION <= DEPRECATED_IN_VERSION or lingering == [], (
        f"Protocol version {PROTOCOL_VERSION} still has deprecated {', '.join(lingering)}"
    )


def test_the_protocol_doc_names_every_deprecated_message_for_removal() -> None:
    section = PROTOCOL_DOC.read_text().split("\n## Deprecated\n", 1)[1].split("\n## ", 1)[0]
    for message in DEPRECATED_MESSAGES:
        assert f"`{message.__name__}`" in section
