"""The runtimes the CLI knows, and the launchers it builds for those a workspace's enabled roles
run on, once their executables are found on PATH."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orbit.agent import ClaudeCliRunner, KiroCliRunner, Role, WorkspaceUnavailable
from orbit.agent.domain.runtimes import CLAUDE, KIRO, RUNTIME_NAMES
from orbit.cli.runtimes import RUNTIMES, build_runtimes
from orbit.protocol import CODER, PLANNER
from orbit.telemetry import AgentCollector
from tests.agent_fakes import ROLES
from tests.cli_fakes import FAKE_RUNTIME, FakeRuntimeLauncher
from tests.stand_ins import stand_in

NOT_ON_PATH = (
    "Roles planner, experimenter, coder, reviewer, super-reviewer, configurator use the runtime"
    " claude, but claude isn't on PATH. Install Claude Code, or set those roles' runtime to one"
    " you have."
)


def on(runtime: str, *names: str) -> dict[str, Role]:
    """The roles of :data:`ROLES`, those named ``names`` running on ``runtime``."""
    return {
        name: role.model_copy(update={"runtime": runtime}) if name in names else role
        for name, role in ROLES.items()
    }


def disabled(roles: dict[str, Role], *names: str) -> dict[str, Role]:
    """``roles``, those named ``names`` disabled."""
    return {
        name: role.model_copy(update={"enabled": False}) if name in names else role
        for name, role in roles.items()
    }


def test_the_cli_knows_the_runtimes_this_version_of_orbit_does() -> None:
    assert tuple(RUNTIMES) == RUNTIME_NAMES


def test_claude_is_claude_codes_cli_on_path() -> None:
    claude = RUNTIMES[CLAUDE]
    assert (claude.executable, claude.install) == ("claude", "Install Claude Code")
    assert isinstance(claude.launcher(AgentCollector()), ClaudeCliRunner)


def test_kiro_is_kiros_cli_on_path() -> None:
    kiro = RUNTIMES[KIRO]
    assert (kiro.executable, kiro.install) == ("kiro-cli", "Install the Kiro CLI and log in")


def test_kiro_is_built_once_its_cli_is_found_sweeping_away_leftover_configs(
    monkeypatch: pytest.MonkeyPatch, stand_in_bin: Path, tmp_path: Path
) -> None:
    kiro_bin = tmp_path / "kiro-bin"
    stand_in(kiro_bin, "kiro-cli")
    monkeypatch.setenv("PATH", f"{kiro_bin}{os.pathsep}{stand_in_bin}")
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / ".kiro" / "agents"
    agents.mkdir(parents=True)
    leftover, fresh = agents / "orbit-agent-1.json", agents / "orbit-agent-2.json"
    leftover.write_text("{}")
    fresh.write_text("{}")
    stale = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(leftover, (stale, stale))

    launchers = build_runtimes(on(KIRO, CODER))
    assert list(launchers) == [CLAUDE, KIRO]
    assert isinstance(launchers[KIRO], KiroCliRunner)
    assert sorted(agents.iterdir()) == [fresh]


def test_kiros_missing_cli_names_its_roles_and_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, stand_in_bin: Path
) -> None:
    monkeypatch.setenv("PATH", str(stand_in_bin))
    with pytest.raises(WorkspaceUnavailable) as raised:
        build_runtimes(on(KIRO, CODER, PLANNER))
    assert str(raised.value) == (
        "Roles planner, coder use the runtime kiro, but kiro-cli isn't on PATH. Install the Kiro"
        " CLI and log in, or set those roles' runtime to one you have."
    )


def test_the_runtime_the_enabled_roles_use_is_built() -> None:
    launchers = build_runtimes(ROLES)
    assert list(launchers) == [CLAUDE]
    assert isinstance(launchers[CLAUDE], ClaudeCliRunner)


def test_a_runtime_whose_executable_isnt_on_path_names_the_roles_using_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(WorkspaceUnavailable) as raised:
        build_runtimes(ROLES)
    assert str(raised.value) == NOT_ON_PATH


def test_only_the_enabled_roles_using_a_missing_runtime_are_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    roles = disabled(ROLES, "experimenter", "reviewer", "super-reviewer", "configurator")
    with pytest.raises(WorkspaceUnavailable, match=r"^Roles planner, coder use the runtime"):
        build_runtimes(roles)


def test_each_runtime_some_enabled_role_uses_is_built(fake_runtime: Path) -> None:
    launchers = build_runtimes(on(FAKE_RUNTIME, CODER))
    assert list(launchers) == [CLAUDE, FAKE_RUNTIME]
    assert isinstance(launchers[FAKE_RUNTIME], FakeRuntimeLauncher)


def test_a_runtime_no_enabled_role_uses_isnt_looked_for_or_built(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(fake_runtime))
    roles = disabled(on(FAKE_RUNTIME, *ROLES), PLANNER)
    roles[PLANNER] = roles[PLANNER].model_copy(update={"runtime": CLAUDE})
    assert list(build_runtimes(roles)) == [FAKE_RUNTIME]


def test_a_second_runtimes_missing_executable_names_its_roles_and_how_to_install_it(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch, stand_in_bin: Path
) -> None:
    monkeypatch.setenv("PATH", str(stand_in_bin))
    with pytest.raises(WorkspaceUnavailable) as raised:
        build_runtimes(on(FAKE_RUNTIME, CODER))
    assert str(raised.value) == (
        "Roles coder use the runtime fake, but fake-agent isn't on PATH. Install the fake, or set"
        " those roles' runtime to one you have."
    )
