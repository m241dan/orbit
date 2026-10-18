"""The token frontends show ``orbit serve``: made on first use, owner-only, and reused."""

import stat
from pathlib import Path

import pytest

from orbit.agent.external.private_files import write_private
from orbit.agent.external.token_file import TokenError, TokenFile


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_token_is_made_owner_only_in_an_owner_only_directory(tmp_path: Path) -> None:
    config = tmp_path / "config"
    token = TokenFile(config).token()
    assert len(token) >= 43
    assert (config / "token").read_text() == token + "\n"
    assert (mode(config / "token"), mode(config)) == (0o600, 0o700)


def test_the_token_is_reused_on_the_next_start(tmp_path: Path) -> None:
    first = TokenFile(tmp_path).token()
    assert TokenFile(tmp_path).token() == first


def test_each_new_token_is_random(tmp_path: Path) -> None:
    assert TokenFile(tmp_path / "a").token() != TokenFile(tmp_path / "b").token()


def test_a_token_file_others_can_read_is_refused_with_the_fix(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("secret\n")
    path.chmod(0o644)
    with pytest.raises(TokenError, match=f"{path} can be read by others; run chmod 600 {path}"):
        TokenFile(tmp_path).token()


def test_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("\n")
    path.chmod(0o600)
    with pytest.raises(TokenError, match=f"{path} is empty"):
        TokenFile(tmp_path).token()


def test_a_token_file_that_cant_be_read_is_refused(tmp_path: Path) -> None:
    (tmp_path / "token").mkdir(mode=0o700)
    with pytest.raises(TokenError, match="Can't read the token file"):
        TokenFile(tmp_path).token()


def test_a_token_file_that_cant_be_looked_at_is_refused(tmp_path: Path) -> None:
    (tmp_path / "config").write_text("not a directory")
    with pytest.raises(TokenError, match="Can't read the token file"):
        TokenFile(tmp_path / "config").token()


def test_a_token_that_cant_be_written_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unwritable(path: Path, text: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("orbit.agent.external.token_file.write_private", unwritable)
    with pytest.raises(TokenError, match=r"Can't write the token file .*: read-only"):
        TokenFile(tmp_path).token()


def test_a_private_file_that_cant_replace_its_target_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "target").mkdir()
    with pytest.raises(IsADirectoryError):
        write_private(tmp_path / "target", "text")
    assert [p.name for p in tmp_path.iterdir()] == ["target"]
