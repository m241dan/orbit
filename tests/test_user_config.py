"""The user's own config file."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import Layer, RoleFields, UserRoles
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.user_config import read_storage, read_user_roles

BUILT_IN_ROLES = read_built_in_roles()

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
    roles = read_user_roles(tmp_path / "missing", BUILT_IN_ROLES)
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
    assert read_user_roles(tmp_path, BUILT_IN_ROLES) == UserRoles(
        Layer("user", str(path)), RoleFields(model="haiku"), {"coder": RoleFields(effort="max")}
    )


def test_an_invalid_role_field_names_the_file_and_the_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[roles]\neffort = "extreme"\n')
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: roles.effort: "):
        read_user_roles(tmp_path, BUILT_IN_ROLES)


def test_the_storage_settings_ignore_the_role_fields(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[roles]\nmodel = "haiku"\n')
    assert read_storage(tmp_path) == StorageSettings()


HELPER = """
[roles.helper]
instructions = "roles/helper.md"
lane = "helper"
workspace = "snapshot"
merge = false
verify = false
"""
"""A personal role whose instructions are in a file under the config directory."""


def test_a_personal_roles_instructions_file_is_read_from_the_config_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text(HELPER)
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "helper.md").write_text("You help.\n", encoding="utf-8")
    helper = read_user_roles(tmp_path, BUILT_IN_ROLES).personal["helper"]
    assert (helper.instructions, helper.lane) == ("You help.", "helper")


def test_a_missing_personal_instructions_file_names_both_files_and_the_role(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(HELPER)
    with pytest.raises(ConfigError) as raised:
        read_user_roles(tmp_path, BUILT_IN_ROLES)
    message = str(raised.value)
    assert message.startswith(f"{path}: roles.helper.instructions: ")
    assert str(tmp_path / "roles" / "helper.md") in message


def test_a_built_in_roles_instructions_in_the_user_config_belong_in_the_project_unread(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[roles.coder]\ninstructions = "missing.md"\n')
    with pytest.raises(ConfigError) as raised:
        read_user_roles(tmp_path, BUILT_IN_ROLES)
    assert str(raised.value).startswith(
        f"{path}: roles.coder.instructions: belongs in the project's .orbit/config.toml"
    )
