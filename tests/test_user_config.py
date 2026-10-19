"""The user's own config file."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.user_config import read_storage

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
