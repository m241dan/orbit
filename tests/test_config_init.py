"""Starting a project's ``.orbit/config.toml`` from the reference config: written where there's
none, refused over one, and with ``force`` replacing it after keeping it as a numbered backup."""

from pathlib import Path

import pytest

from orbit.agent.external.config_file import CONFIG_PATH
from orbit.agent.external.config_init import REFERENCE, ConfigExists, InitResult, init_config
from orbit.agent.external.reference_config import reference_config


def write(root: Path, text: str) -> Path:
    """The project file of the workspace at ``root``, holding ``text``."""
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_the_shipped_reference_is_the_reference_config() -> None:
    assert REFERENCE.read_text(encoding="utf-8") == reference_config()


def test_without_a_file_the_reference_is_written_in_a_new_orbit_directory(tmp_path: Path) -> None:
    result = init_config(tmp_path, force=False)
    path = tmp_path / CONFIG_PATH
    assert result == InitResult(path)
    assert path.read_text(encoding="utf-8") == reference_config()
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_an_existing_file_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    with pytest.raises(ConfigExists) as raised:
        init_config(tmp_path, force=False)
    assert raised.value.path == path
    assert path.read_text() == "fix_attempts = 3\n"
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_force_keeps_the_existing_file_as_the_first_backup(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    result = init_config(tmp_path, force=True)
    backup = path.with_name("config.toml.bak.1")
    assert result == InitResult(path, backup)
    assert backup.read_text() == "fix_attempts = 3\n"
    assert path.read_text(encoding="utf-8") == reference_config()


def test_force_twice_keeps_both_earlier_files(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    init_config(tmp_path, force=True)
    result = init_config(tmp_path, force=True)
    second = path.with_name("config.toml.bak.2")
    assert result == InitResult(path, second)
    assert path.with_name("config.toml.bak.1").read_text() == "fix_attempts = 3\n"
    assert second.read_text(encoding="utf-8") == reference_config()
    assert path.read_text(encoding="utf-8") == reference_config()


def test_force_takes_the_first_backup_number_that_is_free(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    path.with_name("config.toml.bak.1").write_text("older\n")
    result = init_config(tmp_path, force=True)
    second = path.with_name("config.toml.bak.2")
    assert result == InitResult(path, second)
    assert second.read_text() == "fix_attempts = 3\n"
    assert path.with_name("config.toml.bak.1").read_text() == "older\n"


def test_force_without_a_file_writes_the_reference_with_no_backup(tmp_path: Path) -> None:
    result = init_config(tmp_path, force=True)
    path = tmp_path / CONFIG_PATH
    assert result == InitResult(path)
    assert path.read_text(encoding="utf-8") == reference_config()
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_an_orbit_directory_that_is_a_file_raises(tmp_path: Path) -> None:
    (tmp_path / ".orbit").write_text("")
    with pytest.raises(OSError):
        init_config(tmp_path, force=False)
