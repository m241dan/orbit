"""The token frontends show ``orbit serve``: made on first use, owner-only, and reused."""

import stat
from pathlib import Path

import pytest

from orbit.agent.external.private_files import create_private, write_private
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

    monkeypatch.setattr("orbit.agent.external.token_file.create_private", unwritable)
    with pytest.raises(TokenError, match=r"Can't write the token file .*: read-only"):
        TokenFile(tmp_path).token()


def test_rotating_replaces_the_token_owner_only(tmp_path: Path) -> None:
    tokens = TokenFile(tmp_path)
    old = tokens.token()
    tokens.rotate()
    new = tokens.token()
    assert new != old and len(new) >= 43
    assert (tmp_path / "token").read_text() == new + "\n"
    assert mode(tmp_path / "token") == 0o600


def test_rotating_makes_a_token_where_there_was_none(tmp_path: Path) -> None:
    config = tmp_path / "config"
    TokenFile(config).rotate()
    assert len(TokenFile(config).token()) >= 43
    assert (mode(config / "token"), mode(config)) == (0o600, 0o700)


def test_rotating_replaces_a_token_file_others_could_read(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("secret\n")
    path.chmod(0o644)
    TokenFile(tmp_path).rotate()
    assert mode(path) == 0o600 and path.read_text() != "secret\n"


def test_a_rotation_that_cant_be_written_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unwritable(path: Path, text: str) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("orbit.agent.external.token_file.write_private", unwritable)
    with pytest.raises(TokenError, match=r"Can't write the token file .*: read-only"):
        TokenFile(tmp_path).rotate()


def test_a_private_file_that_cant_replace_its_target_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "target").mkdir()
    with pytest.raises(IsADirectoryError):
        write_private(tmp_path / "target", "text")
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


@pytest.mark.parametrize("directory_mode", [0o770, 0o707, 0o777])
def test_a_token_in_a_directory_others_can_write_is_refused_with_the_fix(
    tmp_path: Path, directory_mode: int
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    TokenFile(config).token()
    config.chmod(directory_mode)
    with pytest.raises(
        TokenError, match=f"directory {config} can be written by others; run chmod 700 {config}"
    ):
        TokenFile(config).token()


def test_no_token_is_made_in_a_directory_others_can_write(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir(mode=0o775)
    config.chmod(0o775)
    with pytest.raises(TokenError, match="chmod 700"):
        TokenFile(config).token()
    assert not (config / "token").exists()


def test_a_token_in_a_directory_others_can_only_read_is_accepted(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(0o755)
    token = TokenFile(config).token()
    assert TokenFile(config).token() == token


def test_a_token_directory_that_cant_be_looked_at_is_refused(tmp_path: Path) -> None:
    (tmp_path / "config").write_text("not a directory")
    with pytest.raises(TokenError, match="Can't read the token file"):
        TokenFile(tmp_path / "config" / "orbit").token()


def test_the_first_token_written_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "token"

    def raced(nbytes: int) -> str:
        path.write_text("theirs\n")
        path.chmod(0o600)
        return "ours"

    monkeypatch.setattr("orbit.agent.external.token_file.secrets.token_urlsafe", raced)
    assert TokenFile(tmp_path).token() == "theirs"
    assert path.read_text() == "theirs\n"
    assert [p.name for p in tmp_path.iterdir()] == ["token"]


def test_a_private_file_that_exists_is_not_replaced_by_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "target"
    path.write_text("first")
    with pytest.raises(FileExistsError):
        create_private(path, "second")
    assert path.read_text() == "first"
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


def test_a_private_file_is_created_owner_only_in_an_owner_only_directory(tmp_path: Path) -> None:
    path = tmp_path / "private" / "target"
    create_private(path, "text")
    assert path.read_text() == "text"
    assert (mode(path), mode(path.parent)) == (0o600, 0o700)
    assert [p.name for p in path.parent.iterdir()] == ["target"]


def test_a_private_file_that_cant_be_written_leaves_no_temporary_file(tmp_path: Path) -> None:
    with pytest.raises(UnicodeEncodeError):
        write_private(tmp_path / "target", "\ud800")
    assert list(tmp_path.iterdir()) == []
