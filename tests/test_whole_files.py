"""Files written whole or not at all: the old file survives a failed write, with no temporary
file left behind, and modes are kept, given or made private."""

import os
import stat
from pathlib import Path

import pytest

from orbit.agent.external.whole_files import (
    create_private,
    create_whole,
    write_private,
    write_whole,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_whole_file_replaces_the_old_one(tmp_path: Path) -> None:
    path = tmp_path / "target"
    path.write_text("old")
    write_whole(path, "new")
    assert path.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


def test_a_whole_file_keeps_the_mode_of_the_one_it_replaces(tmp_path: Path) -> None:
    path = tmp_path / "target"
    path.write_text("old")
    path.chmod(0o751)
    write_whole(path, "new")
    assert mode(path) == 0o751


def test_a_new_whole_file_gets_the_umasks_mode(tmp_path: Path) -> None:
    umask = os.umask(0o027)
    try:
        write_whole(tmp_path / "target", "text")
    finally:
        os.umask(umask)
    assert mode(tmp_path / "target") == 0o640


def test_a_whole_file_takes_the_mode_its_given(tmp_path: Path) -> None:
    path = tmp_path / "target"
    path.write_text("old")
    path.chmod(0o644)
    write_whole(path, "new", 0o700)
    assert mode(path) == 0o700


def test_a_whole_file_that_cant_replace_the_old_one_leaves_it_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "target"
    path.write_text("old")

    def full(source: Path, target: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError, match="No space"):
        write_whole(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


def test_a_temporary_file_whose_mode_cant_be_set_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(descriptor: int, mode: int) -> None:
        raise PermissionError("refused")

    monkeypatch.setattr(os, "fchmod", refused)
    with pytest.raises(PermissionError, match="refused"):
        write_whole(tmp_path / "target", "text", 0o600)
    assert list(tmp_path.iterdir()) == []


def test_a_created_whole_file_gets_the_umasks_mode(tmp_path: Path) -> None:
    umask = os.umask(0o027)
    try:
        create_whole(tmp_path / "target", "text")
    finally:
        os.umask(umask)
    assert (tmp_path / "target").read_text() == "text"
    assert mode(tmp_path / "target") == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


def test_a_whole_file_that_exists_is_not_replaced_by_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "target"
    path.write_text("first")
    with pytest.raises(FileExistsError):
        create_whole(path, "second")
    assert path.read_text() == "first"
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


def test_a_private_file_is_owner_only_even_over_an_open_one(tmp_path: Path) -> None:
    path = tmp_path / "private" / "target"
    path.parent.mkdir()
    path.write_text("old")
    path.chmod(0o644)
    write_private(path, "new")
    assert (path.read_text(), mode(path)) == ("new", 0o600)


def test_a_private_files_directory_is_made_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "private" / "target"
    write_private(path, "text")
    assert (mode(path), mode(path.parent)) == (0o600, 0o700)


def test_a_private_file_that_cant_replace_its_target_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "target").mkdir()
    with pytest.raises(IsADirectoryError):
        write_private(tmp_path / "target", "text")
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


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
