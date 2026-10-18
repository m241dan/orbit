"""The deprecated messages: listed, documented, and gone by the next major protocol version."""

from pathlib import Path
from typing import Any, get_args

from orbit.protocol import (
    DEPRECATED_MESSAGES,
    PROTOCOL_VERSION,
    Command,
    Event,
    ListSessions,
    OpenSession,
    SessionsListed,
)

PROTOCOL_DOC = Path(__file__).parent.parent / "docs" / "protocol.md"


def members(union: Any) -> set[type]:
    """The message classes in the discriminated ``union``."""
    return set(get_args(get_args(union)[0]))


def test_the_deprecated_messages_are_the_old_session_commands_and_listing() -> None:
    assert (OpenSession, ListSessions, SessionsListed) == DEPRECATED_MESSAGES


def test_deprecated_messages_are_removed_past_protocol_version_1() -> None:
    present = members(Command) | members(Event)
    lingering = [message.__name__ for message in DEPRECATED_MESSAGES if message in present]
    assert PROTOCOL_VERSION <= 1 or lingering == [], (
        f"Protocol version {PROTOCOL_VERSION} still has deprecated {', '.join(lingering)}"
    )


def test_the_protocol_doc_names_every_deprecated_message_for_removal() -> None:
    section = PROTOCOL_DOC.read_text().split("\n## Deprecated\n", 1)[1].split("\n## ", 1)[0]
    for message in DEPRECATED_MESSAGES:
        assert f"`{message.__name__}`" in section
