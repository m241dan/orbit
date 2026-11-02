"""``credentials.toml`` in orbit's config directory, read afresh at each lookup: owner-only, a
flat table of strings, and refused, saying why, otherwise."""

from pathlib import Path

import pytest

from orbit.agent.application.ports import CredentialsUnreadable
from orbit.agent.external.credentials_file import CredentialsFile


def write_credentials(config_dir: Path, text: str, mode: int = 0o600) -> Path:
    """``credentials.toml`` in ``config_dir``, holding ``text``, with ``mode``."""
    path = config_dir / "credentials.toml"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_a_variable_the_file_sets_has_its_value(tmp_path: Path) -> None:
    write_credentials(tmp_path, 'GITHUB_TOKEN = "secret"\n')
    assert CredentialsFile(tmp_path)("GITHUB_TOKEN") == "secret"


def test_a_variable_the_file_doesnt_set_has_none(tmp_path: Path) -> None:
    write_credentials(tmp_path, 'GITHUB_TOKEN = "secret"\n')
    assert CredentialsFile(tmp_path)("SEMGREP_TOKEN") is None


def test_a_missing_file_sets_nothing(tmp_path: Path) -> None:
    assert CredentialsFile(tmp_path)("GITHUB_TOKEN") is None


def test_a_missing_config_directory_sets_nothing(tmp_path: Path) -> None:
    assert CredentialsFile(tmp_path / "config")("GITHUB_TOKEN") is None


@pytest.mark.parametrize("mode", [0o777, 0o775])
def test_a_missing_file_in_a_directory_others_can_write_sets_nothing(
    tmp_path: Path, mode: int
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(mode)
    assert CredentialsFile(config)("GITHUB_TOKEN") is None


def test_the_file_is_read_afresh_at_each_lookup(tmp_path: Path) -> None:
    credentials = CredentialsFile(tmp_path)
    write_credentials(tmp_path, 'GITHUB_TOKEN = "old"\n')
    assert credentials("GITHUB_TOKEN") == "old"
    write_credentials(tmp_path, 'GITHUB_TOKEN = "new"\n')
    assert credentials("GITHUB_TOKEN") == "new"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o620, 0o602])
def test_a_file_others_can_read_or_write_is_refused_with_the_fix(tmp_path: Path, mode: int) -> None:
    path = write_credentials(tmp_path, 'GITHUB_TOKEN = "secret"\n', mode)
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(tmp_path)("GITHUB_TOKEN")
    assert str(raised.value) == f"{path} must be private: chmod 600 it."


def test_a_directory_others_can_write_is_refused_with_the_fix(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(0o777)
    write_credentials(config, 'GITHUB_TOKEN = "secret"\n')
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(config)("GITHUB_TOKEN")
    assert str(raised.value) == (
        f"{config} holds credentials.toml, so others mustn't write to it: chmod 700 it."
    )


def test_malformed_toml_is_refused_naming_the_file_and_line(tmp_path: Path) -> None:
    path = write_credentials(tmp_path, 'GITHUB_TOKEN = "secret"\nSEMGREP_TOKEN secret\n')
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(tmp_path)("GITHUB_TOKEN")
    message = str(raised.value)
    assert message.startswith(f"{path} isn't valid TOML: ")
    assert "line 2" in message
    assert "secret" not in message


def test_a_value_that_isnt_a_string_is_refused_naming_its_key(tmp_path: Path) -> None:
    path = write_credentials(tmp_path, 'GITHUB_TOKEN = "secret"\nPORT = 8080\n')
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(tmp_path)("GITHUB_TOKEN")
    assert str(raised.value) == f"PORT in {path} isn't a string."


def test_a_nested_table_is_refused_naming_its_key(tmp_path: Path) -> None:
    path = write_credentials(tmp_path, '[github]\nTOKEN = "secret"\n')
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(tmp_path)("TOKEN")
    assert str(raised.value) == f"github in {path} isn't a string."


def test_a_file_that_isnt_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    path.write_bytes(b'GITHUB_TOKEN = "\xff"\n')
    path.chmod(0o600)
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(tmp_path)("GITHUB_TOKEN")
    assert str(raised.value) == f"Can't read {path}: it isn't UTF-8."


def test_a_file_that_cant_be_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    path.mkdir(mode=0o700)
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(tmp_path)("GITHUB_TOKEN")
    assert str(raised.value) == f"Can't read {path}: Is a directory."


def test_a_config_directory_that_cant_be_looked_at_is_refused(tmp_path: Path) -> None:
    (tmp_path / "config").write_text("not a directory")
    config = tmp_path / "config" / "orbit"
    with pytest.raises(CredentialsUnreadable) as raised:
        CredentialsFile(config)("GITHUB_TOKEN")
    assert str(raised.value) == f"Can't read {config / 'credentials.toml'}: Not a directory."
