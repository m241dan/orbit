"""Credentials MCP servers' environments are filled from: the contract orbit's environment and
the fake both keep."""

from collections.abc import Callable, Mapping

import pytest

from orbit.agent.application.ports import Credentials, CredentialsUnreadable
from orbit.agent.external.environment_credentials import EnvironmentCredentials
from tests.mcp_fakes import FakeCredentials

type MakeCredentials = Callable[[Mapping[str, str]], Credentials]
"""Makes credentials holding the values given, by variable."""


@pytest.fixture(params=["environment", "fake"])
def credentials_holding(request: pytest.FixtureRequest) -> MakeCredentials:
    """Makes orbit's environment credentials, over a table, or the fake."""
    if request.param == "environment":
        return lambda values: EnvironmentCredentials(dict(values))
    return lambda values: FakeCredentials(values)


def test_a_variable_set_has_its_value(credentials_holding: MakeCredentials) -> None:
    credentials = credentials_holding({"GITHUB_TOKEN": "secret"})
    assert credentials("GITHUB_TOKEN") == "secret"


def test_a_variable_not_set_has_none(credentials_holding: MakeCredentials) -> None:
    assert credentials_holding({})("GITHUB_TOKEN") is None


def test_orbits_environment_is_read_as_it_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = EnvironmentCredentials()
    monkeypatch.setenv("ORBIT_TEST_TOKEN", "fresh")
    assert credentials("ORBIT_TEST_TOKEN") == "fresh"


def test_unreadable_fake_credentials_say_why() -> None:
    with pytest.raises(CredentialsUnreadable, match="must be private"):
        FakeCredentials({}, unreadable="credentials.toml must be private")("GITHUB_TOKEN")
