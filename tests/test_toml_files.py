"""TOML files read for their settings."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.external.toml_files import read_toml


def as_is(table: dict[str, Any]) -> dict[str, Any]:
    return table


def refuse(table: dict[str, Any]) -> dict[str, Any]:
    raise ConfigError("name: not allowed")


def test_the_file_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "a.toml"
    path.write_text("name = 'orbit'\n", encoding="utf-8")
    assert read_toml(path, as_is) == {"name": "orbit"}


def test_utf8_text_is_read_whatever_the_locale(tmp_path: Path) -> None:
    path = tmp_path / "a.toml"
    path.write_text("name = 'Zoë ✓'\n", encoding="utf-8")
    script = (
        "import sys; from pathlib import Path;"
        " from orbit.agent.external.toml_files import read_toml;"
        " assert read_toml(Path(sys.argv[1]), dict) == {'name': 'Zo\\u00eb \\u2713'}"
    )
    ascii_locale = {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    env = {**os.environ, **ascii_locale}
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, env=env)


@pytest.mark.parametrize(
    "content", [b"name = = 1\n", b"name = 'a' # \xff\n"], ids=["invalid TOML", "not UTF-8"]
)
def test_an_unreadable_file_raises_config_error_naming_it(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "a.toml"
    path.write_bytes(content)
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: "):
        read_toml(path, as_is)


def test_a_directory_raises_config_error_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=f"^{re.escape(str(tmp_path))}: "):
        read_toml(tmp_path, as_is)


def test_settings_the_loader_refuses_raise_config_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "a.toml"
    path.write_text("name = 'orbit'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: name: not allowed$"):
        read_toml(path, refuse)
