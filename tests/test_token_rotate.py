"""``orbit token rotate``: a new token in the token file, never shown."""

import logging
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orbit.agent import TokenFile
from orbit.cli import app, serve

runner = CliRunner()


def test_rotate_writes_a_new_owner_only_token_and_names_the_file_not_the_token(
    tmp_path: Path,
) -> None:
    old = TokenFile(tmp_path).token()
    result = runner.invoke(app, ["token", "rotate", "--config-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    path = tmp_path / "token"
    new = path.read_text().strip()
    assert new != old
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f"Rotated the token in {path}" in result.stdout
    assert new not in result.output and old not in result.output


def test_rotate_uses_the_config_directory_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(tmp_path / "config"))
    result = runner.invoke(app, ["token", "rotate"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "config" / "token").exists()


def test_a_rotation_that_cant_be_written_stops_with_the_reason(tmp_path: Path) -> None:
    (tmp_path / "config").write_text("not a directory")
    result = runner.invoke(app, ["token", "rotate", "--config-dir", str(tmp_path / "config")])

    assert result.exit_code == 2
    assert "Can't write the token file" in result.stderr


def test_the_served_token_is_the_one_in_the_file_now(tmp_path: Path) -> None:
    tokens = TokenFile(tmp_path)
    current = serve._current_token(tokens)
    old = current()
    tokens.rotate()
    assert current() == tokens.token() != old


def test_a_token_file_unusable_while_serving_admits_no_one_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tokens = TokenFile(tmp_path)
    tokens.token()
    tokens.path.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        assert serve._current_token(tokens)() == ""
    assert f"chmod 600 {tokens.path}" in caplog.text
