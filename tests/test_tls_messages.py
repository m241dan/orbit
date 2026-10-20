"""The server's and the client's TLS and connection messages: each says what's wrong in its
first sentence, which README.md's table of TLS and connection errors lists."""

from pathlib import Path
from types import ModuleType

import pytest

from orbit.agent.external import tls_messages
from orbit.client import messages

README = Path(__file__).parent.parent / "README.md"


def _first_sentence(message: str) -> str:
    """What ``message`` says is wrong: its first sentence."""
    return message.split(". ", 1)[0] + "."


@pytest.mark.parametrize("message", tls_messages.ALL + messages.ALL)
def test_each_messages_first_sentence_is_in_the_readmes_error_table(message: str) -> None:
    first = _first_sentence(message)
    assert "{" not in first
    rows = [
        line for line in README.read_text(encoding="utf-8").splitlines() if line.startswith("|")
    ]
    assert any(f"| {first} |" in row for row in rows), f"{first!r} is missing from README.md"


@pytest.mark.parametrize("module", [tls_messages, messages])
def test_every_message_is_listed(module: ModuleType) -> None:
    constants = [value for name, value in vars(module).items() if name.isupper() and name != "ALL"]
    assert sorted(constants) == sorted(module.ALL)
