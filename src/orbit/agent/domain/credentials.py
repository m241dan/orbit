"""The rules of ``credentials.toml``, the file in orbit's config directory that fills in MCP
servers' variables orbit's environment doesn't set: what it may hold, and the wording of its
refusals."""

from collections.abc import Mapping
from dataclasses import dataclass

from orbit.agent.domain.mcp_proxy import UnsetVariable

CREDENTIALS_FILE = "credentials.toml"


@dataclass(frozen=True)
class NonStringCredential:
    """A ``key`` in the file whose value isn't a string."""

    key: str


def parse_credentials(table: Mapping[str, object]) -> dict[str, str] | NonStringCredential:
    """The variables ``table``, the file's TOML, sets, by name, or the first of its keys whose
    value isn't a string."""
    values = {key: value for key, value in table.items() if isinstance(value, str)}
    wrong = next((key for key in table if key not in values), None)
    return values if wrong is None else NonStringCredential(wrong)


def word_too_open(path: str) -> str:
    """Why the file at ``path``, which its group or others can read or write, is refused."""
    return f"{path} must be private: chmod 600 it."


def word_directory_too_open(directory: str) -> str:
    """Why the file in ``directory``, which others can write, is refused."""
    return f"{directory} holds {CREDENTIALS_FILE}, so others mustn't write to it: chmod 700 it."


def word_invalid_toml(path: str, detail: str) -> str:
    """Why the file at ``path``, which isn't valid TOML as ``detail`` says, is refused."""
    return f"{path} isn't valid TOML: {detail}."


def word_not_a_string(path: str, wrong: NonStringCredential) -> str:
    """Why the file at ``path``, whose value for ``wrong``'s key isn't a string, is refused."""
    return f"{wrong.key} in {path} isn't a string."


def word_unreadable(path: str, detail: str) -> str:
    """Why the file at ``path``, which can't be read as ``detail`` says, is refused."""
    return f"Can't read {path}: {detail}."


def word_unset_because(name: str, reason: str) -> str:
    """Why a server needing the variable ``name``, which orbit's environment doesn't set, can't
    start, the file being refused for ``reason``."""
    return f"{UnsetVariable(name).message} {reason}"
