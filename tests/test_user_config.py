"""The user's own config file."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import Layer, RoleFields, UserRoles
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.user_config import read_storage, read_user_roles

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


# The role fields


def test_without_a_file_no_role_fields_are_set(tmp_path: Path) -> None:
    roles = read_user_roles(tmp_path / "missing")
    assert (roles.every, roles.named) == (RoleFields(), {})


def test_the_files_role_fields_are_read_as_its_layers(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[storage]
tool_output_kb = 1

[roles]
model = "haiku"

[roles.coder]
effort = "max"
"""
    )
    assert read_user_roles(tmp_path) == UserRoles(
        Layer("user", str(path)), RoleFields(model="haiku"), {"coder": RoleFields(effort="max")}
    )


def test_an_invalid_role_field_names_the_file_and_the_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[roles]\neffort = "extreme"\n')
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: roles.effort: "):
        read_user_roles(tmp_path)


def test_the_storage_settings_ignore_the_role_fields(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[roles]\nmodel = "haiku"\n')
    assert read_storage(tmp_path) == StorageSettings()
