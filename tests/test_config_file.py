from pathlib import Path

import pytest

from orbit.agent.domain.roles import BUILT_IN_ROLES, ConfigError, OrbitConfig
from orbit.agent.external.config_file import CONFIG_PATH, read_config


def write(root: Path, text: str) -> Path:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_without_a_file_the_defaults_apply(tmp_path: Path) -> None:
    assert read_config(str(tmp_path)) == OrbitConfig()


def test_the_file_is_read_over_the_defaults(tmp_path: Path) -> None:
    write(
        tmp_path,
        """
fix_attempts = 3

[roles.coder]
model = "sonnet"
timeout_minutes = 45
""",
    )
    config = read_config(str(tmp_path))
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(ConfigError, match=str(path)):
        read_config(str(tmp_path))


def test_invalid_settings_name_the_file_and_the_problem(tmp_path: Path) -> None:
    path = write(tmp_path, "[roles.coder]\ntools = ['launch_rockets']\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path))
    assert str(path) in str(raised.value)
    assert "roles.coder.tools" in str(raised.value)
