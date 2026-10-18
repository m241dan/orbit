"""The user's own config file and the directory it's in."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.user_config import read_storage, resolve_config_dir

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


# The storage settings


def test_without_a_file_the_storage_defaults_apply(tmp_path: Path) -> None:
    assert read_storage(tmp_path / "missing") == StorageSettings()


def test_the_files_storage_is_read(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[storage]\ntool_output_kb = 1\n\n[server]\nport = 1\n")
    assert read_storage(tmp_path) == StorageSettings(tool_output_kb=1)


def test_an_invalid_setting_names_the_file_and_the_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[storage]\ntool_output = 3\n")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: storage.tool_output: "):
        read_storage(tmp_path)


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[storage\n")
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_storage(tmp_path)
