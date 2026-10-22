"""Reading the server host's directories: directories alone, symlinks to them included, marked
as repositories, and the errors reading them raises."""

from pathlib import Path

import pytest

from orbit.agent import DirectoryLister
from orbit.agent.domain.listings import FoundDirectory


def tree(root: Path) -> None:
    """``b``, a repository, ``a``, ``.hidden``, a file and ``l``, a symlink to ``a``, in
    ``root``."""
    (root / "b" / ".git").mkdir(parents=True)
    (root / "a").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("")
    (root / "l").symlink_to("a")


async def test_only_directories_are_found_symlinks_to_them_with_their_targets(
    tmp_path: Path,
) -> None:
    tree(tmp_path)
    found = await DirectoryLister().directories(str(tmp_path))
    assert sorted(found, key=lambda directory: directory.name) == [
        FoundDirectory(".hidden", str(tmp_path / ".hidden"), False),
        FoundDirectory("a", str(tmp_path / "a"), False),
        FoundDirectory("b", str(tmp_path / "b"), True),
        FoundDirectory("l", str(tmp_path / "a"), False, "a"),
    ]


async def test_a_symlink_to_a_file_isnt_found(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / "n").symlink_to("notes.txt")
    assert await DirectoryLister().directories(str(tmp_path)) == []


async def test_a_missing_directory_cant_be_read(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await DirectoryLister().directories(str(tmp_path / "gone"))


async def test_a_file_cant_be_read_as_a_directory(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("")
    with pytest.raises(NotADirectoryError):
        await DirectoryLister().directories(str(tmp_path / "notes.txt"))


async def test_a_path_is_resolved_following_its_symlinks_even_past_what_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "l").symlink_to("a")
    resolved = await DirectoryLister().resolved(str(tmp_path / "l" / "gone"))
    assert resolved == str(tmp_path.resolve() / "a" / "gone")
