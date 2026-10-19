"""Where orbit keeps its state and the user's config."""

from pathlib import Path

import pytest

from orbit.agent.external.directories import resolve_config_dir, resolve_state_dir

# The config directory


def test_the_config_directory_flag_wins(tmp_path: Path) -> None:
    environ = {"ORBIT_CONFIG_DIR": "/env/orbit", "XDG_CONFIG_HOME": "/xdg"}
    assert resolve_config_dir(tmp_path / "flag", environ) == tmp_path / "flag"


def test_without_the_flag_the_config_directory_is_orbits_variable() -> None:
    environ = {"ORBIT_CONFIG_DIR": "/env/orbit", "XDG_CONFIG_HOME": "/xdg"}
    assert resolve_config_dir(None, environ) == Path("/env/orbit")


def test_without_orbits_variable_the_config_directory_is_orbit_in_the_xdg_config_home() -> None:
    environ = {"ORBIT_CONFIG_DIR": "", "XDG_CONFIG_HOME": "/xdg"}
    assert resolve_config_dir(None, environ) == Path("/xdg/orbit")


def test_by_default_the_config_directory_is_in_the_home_directory() -> None:
    assert resolve_config_dir(None, {"XDG_CONFIG_HOME": ""}) == Path.home() / ".config/orbit"


def test_the_config_directory_is_expanded_and_made_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_config_dir(Path("config"), {}) == tmp_path / "config"
    assert resolve_config_dir(None, {"ORBIT_CONFIG_DIR": "~/config"}) == Path.home() / "config"


# The state directory


def test_the_state_directory_flag_wins(tmp_path: Path) -> None:
    environ = {"ORBIT_STATE_DIR": "/env/orbit", "XDG_STATE_HOME": "/xdg"}
    assert resolve_state_dir(tmp_path / "flag", environ) == tmp_path / "flag"


def test_without_the_flag_the_state_directory_is_orbits_variable() -> None:
    environ = {"ORBIT_STATE_DIR": "/env/orbit", "XDG_STATE_HOME": "/xdg"}
    assert resolve_state_dir(None, environ) == Path("/env/orbit")


def test_without_orbits_variable_the_state_directory_is_orbit_in_the_xdg_state_home() -> None:
    environ = {"ORBIT_STATE_DIR": "", "XDG_STATE_HOME": "/xdg"}
    assert resolve_state_dir(None, environ) == Path("/xdg/orbit")


def test_by_default_the_state_directory_is_in_the_home_directory() -> None:
    assert resolve_state_dir(None, {"XDG_STATE_HOME": ""}) == Path.home() / ".local/state/orbit"


def test_the_state_directory_is_expanded_and_made_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_state_dir(Path("state"), {}) == tmp_path / "state"
    assert resolve_state_dir(None, {"ORBIT_STATE_DIR": "~/state"}) == Path.home() / "state"
