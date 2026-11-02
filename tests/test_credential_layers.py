"""Orbit's environment layered over ``credentials.toml``: the environment wins, the file fills
in what it lacks, and a refused file fails only the servers that needed it, saying why."""

from pathlib import Path

import pytest

from orbit.agent.application.credentials import LayeredCredentials
from orbit.agent.application.ports import CredentialsUnreadable
from orbit.agent.application.tool_proxy import Caller, McpProxying, ToolProxy
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import Role, RoleTools
from orbit.agent.external.credentials_file import CredentialsFile
from orbit.agent.external.environment_credentials import EnvironmentCredentials
from orbit.protocol import Event, Notice
from orbit.tracing import NoSpan
from tests.mcp_fakes import FakeConnector, FakeCredentials

GITHUB = McpServer(command=("github-mcp-server", "stdio"), env={"TOKEN": "$GITHUB_TOKEN"})
ROLE = Role(instructions="Work.", tools=RoleTools.model_validate({"mcp": ["github"]}))
CODER = Caller("a1", "coder", ROLE, "/work/a1", NoSpan("a"))
UNSET = "GITHUB_TOKEN isn't set in orbit's environment or credentials.toml."


def write_credentials(config_dir: Path, text: str, mode: int = 0o600) -> Path:
    """``credentials.toml`` in ``config_dir``, holding ``text``, with ``mode``."""
    path = config_dir / "credentials.toml"
    path.write_text(text)
    path.chmod(mode)
    return path


class Started:
    """A proxy of the GitHub server through a fake connector, its variables filled from orbit's
    environment, ``environ``, over the credentials file in ``config_dir``, noting what it
    publishes."""

    def __init__(self, config_dir: Path, environ: dict[str, str] | None = None) -> None:
        self.connector = FakeConnector()
        self.published: list[Event] = []
        credentials = LayeredCredentials(
            EnvironmentCredentials(environ or {}), CredentialsFile(config_dir)
        )
        proxying = McpProxying({"github": GITHUB}, self.connector, credentials, "/state/mcp")
        self.proxy = ToolProxy(proxying, lambda: {"coder": ROLE}, self.publish)

    async def publish(self, event: Event) -> None:
        self.published.append(event)

    @property
    def notices(self) -> list[str]:
        return [event.message for event in self.published if isinstance(event, Notice)]

    @property
    def tokens(self) -> list[str]:
        """The token each start of the server was given, in order."""
        return [opened.env["TOKEN"] for opened in self.connector.opened]

    async def call(self) -> str:
        """The answer to the coder's call to the server's ``get_issue``."""
        reply = await self.proxy.call(CODER, "github__get_issue", {"number": 1})
        return reply.text


# Precedence


def test_a_variable_in_the_environment_wins_over_the_fallback() -> None:
    credentials = LayeredCredentials(
        FakeCredentials({"GITHUB_TOKEN": "env"}), FakeCredentials({"GITHUB_TOKEN": "file"})
    )
    assert credentials("GITHUB_TOKEN") == "env"


def test_a_variable_set_empty_in_the_environment_still_wins() -> None:
    credentials = LayeredCredentials(
        FakeCredentials({"GITHUB_TOKEN": ""}), FakeCredentials({"GITHUB_TOKEN": "file"})
    )
    assert credentials("GITHUB_TOKEN") == ""


def test_a_variable_the_environment_lacks_comes_from_the_fallback() -> None:
    credentials = LayeredCredentials(FakeCredentials({}), FakeCredentials({"GITHUB_TOKEN": "f"}))
    assert credentials("GITHUB_TOKEN") == "f"


def test_an_unreadable_fallback_isnt_read_for_a_variable_the_environment_sets() -> None:
    credentials = LayeredCredentials(
        FakeCredentials({"GITHUB_TOKEN": "env"}), FakeCredentials({}, unreadable="broken")
    )
    assert credentials("GITHUB_TOKEN") == "env"


def test_an_unreadable_fallback_fails_a_variable_the_environment_lacks_saying_both() -> None:
    credentials = LayeredCredentials(
        FakeCredentials({}), FakeCredentials({}, unreadable="x must be private: chmod 600 it.")
    )
    with pytest.raises(CredentialsUnreadable) as raised:
        credentials("GITHUB_TOKEN")
    assert str(raised.value) == f"{UNSET} x must be private: chmod 600 it."


# Servers started


async def test_a_token_only_in_the_file_reaches_the_servers_environment(tmp_path: Path) -> None:
    write_credentials(tmp_path, 'GITHUB_TOKEN = "from-file"\n')
    started = Started(tmp_path)
    assert await started.call() == "Issue 1: it's broken."
    assert started.tokens == ["from-file"]
    assert started.notices == []


async def test_a_token_in_both_comes_from_the_environment(tmp_path: Path) -> None:
    write_credentials(tmp_path, 'GITHUB_TOKEN = "from-file"\n')
    started = Started(tmp_path, {"GITHUB_TOKEN": "from-env"})
    await started.call()
    assert started.tokens == ["from-env"]


async def test_a_file_others_can_read_fails_the_start_with_both_reasons(tmp_path: Path) -> None:
    path = write_credentials(tmp_path, 'GITHUB_TOKEN = "from-file"\n', 0o644)
    started = Started(tmp_path)
    reply = await started.call()
    await started.call()
    reason = f"{UNSET} {path} must be private: chmod 600 it"
    assert reply == f"MCP server github failed: {reason}."
    assert started.notices == [
        f"MCP server github failed: {reason}. Agents' calls to it return errors until it starts."
    ]
    assert started.connector.opened == []


async def test_a_file_others_can_read_is_ignored_for_a_token_in_the_environment(
    tmp_path: Path,
) -> None:
    write_credentials(tmp_path, 'GITHUB_TOKEN = "from-file"\n', 0o644)
    started = Started(tmp_path, {"GITHUB_TOKEN": "from-env"})
    assert await started.call() == "Issue 1: it's broken."
    assert started.tokens == ["from-env"]
    assert started.notices == []


async def test_a_missing_file_fails_the_start_with_the_missing_variable(tmp_path: Path) -> None:
    started = Started(tmp_path)
    assert await started.call() == f"MCP server github failed: {UNSET}"
    assert started.notices == [
        f"MCP server github failed: {UNSET} Agents' calls to it return errors until it starts."
    ]


async def test_a_group_writable_directory_without_a_file_fails_with_the_missing_variable(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(0o775)
    started = Started(config)
    assert await started.call() == f"MCP server github failed: {UNSET}"


async def test_malformed_toml_fails_the_start_naming_the_file(tmp_path: Path) -> None:
    path = write_credentials(tmp_path, "GITHUB_TOKEN from-file\n")
    started = Started(tmp_path)
    reply = await started.call()
    assert reply.startswith(f"MCP server github failed: {UNSET} {path} isn't valid TOML: ")
    assert "line 1" in reply
    assert "from-file" not in "".join(started.notices)


async def test_a_change_to_the_file_between_starts_is_picked_up(tmp_path: Path) -> None:
    write_credentials(tmp_path, 'GITHUB_TOKEN = "first"\n')
    started = Started(tmp_path)
    await started.call()
    write_credentials(tmp_path, 'GITHUB_TOKEN = "second"\n')
    await started.proxy.call(CODER, "github__fail", {})  # Crashes it, so the next call starts it.
    await started.call()
    assert started.tokens == ["first", "second"]
