"""Credentials MCP servers' environments are filled from: the contract orbit's environment,
``credentials.toml``, the two layered and the fake all keep."""

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from orbit.agent.application.credentials import LayeredCredentials
from orbit.agent.application.ports import Credentials, CredentialsUnreadable
from orbit.agent.external.credentials_file import CredentialsFile
from orbit.agent.external.environment_credentials import EnvironmentCredentials
from tests.mcp_fakes import FakeCredentials

type MakeCredentials = Callable[[Mapping[str, str]], Credentials]
"""Makes credentials holding the values given, by variable."""

type MakeUnreadable = Callable[[], Credentials]
"""Makes credentials that can't be read."""


def file_holding(config_dir: Path, values: Mapping[str, str], mode: int = 0o600) -> Path:
    """``config_dir``, its ``credentials.toml`` setting ``values``, with ``mode``."""
    path = config_dir / "credentials.toml"
    path.write_text("".join(f'{name} = "{value}"\n' for name, value in values.items()))
    path.chmod(mode)
    return config_dir


@pytest.fixture(params=["environment", "file", "layered", "fake"])
def credentials_holding(request: pytest.FixtureRequest, tmp_path: Path) -> MakeCredentials:
    """Makes orbit's environment credentials, over a table, the credentials file, the two
    layered, the file's half holding the values, or the fake."""
    if request.param == "environment":
        return lambda values: EnvironmentCredentials(dict(values))
    if request.param == "file":
        return lambda values: CredentialsFile(file_holding(tmp_path, values))
    if request.param == "layered":
        return lambda values: LayeredCredentials(
            EnvironmentCredentials({}), CredentialsFile(file_holding(tmp_path, values))
        )
    return lambda values: FakeCredentials(values)


@pytest.fixture(params=["file", "fake"])
def unreadable_credentials(request: pytest.FixtureRequest, tmp_path: Path) -> MakeUnreadable:
    """Makes a credentials file others can read, or the fake, unreadable."""
    if request.param == "file":
        return lambda: CredentialsFile(file_holding(tmp_path, {"GITHUB_TOKEN": "s"}, 0o644))
    return lambda: FakeCredentials({}, unreadable="credentials.toml must be private")


def test_a_variable_set_has_its_value(credentials_holding: MakeCredentials) -> None:
    credentials = credentials_holding({"GITHUB_TOKEN": "secret"})
    assert credentials("GITHUB_TOKEN") == "secret"


def test_a_variable_not_set_has_none(credentials_holding: MakeCredentials) -> None:
    assert credentials_holding({})("GITHUB_TOKEN") is None


def test_unreadable_credentials_say_why(unreadable_credentials: MakeUnreadable) -> None:
    with pytest.raises(CredentialsUnreadable, match="must be private"):
        unreadable_credentials()("GITHUB_TOKEN")


def test_orbits_environment_is_read_as_it_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = EnvironmentCredentials()
    monkeypatch.setenv("ORBIT_TEST_TOKEN", "fresh")
    assert credentials("ORBIT_TEST_TOKEN") == "fresh"
