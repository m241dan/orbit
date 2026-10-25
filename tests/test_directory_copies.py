"""Copies outside git: the workspace's directory copied, without ``.orbit/`` or ``.git/``, into
the state directory's copies folder, set up there, and removed with everything in it."""

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from orbit.agent.application.ports import TreeFailed
from orbit.agent.domain.checks import Setup
from orbit.agent.external import directory_copies
from orbit.agent.external.directory_copies import (
    DirectoryCopies,
    copies_folder,
    copy_directory,
)

MARK = (sys.executable, "-c", "open('set-up', 'w').write('yes')")
FAIL = (sys.executable, "-c", "import sys; print('no network'); sys.exit(1)")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A plain directory with orbit's project folder, a git directory and a nested one."""
    root = tmp_path / "plain"
    (root / ".orbit").mkdir(parents=True)
    (root / ".orbit" / "config.toml").write_text("[roles]\n")
    (root / ".git").mkdir()
    (root / "vendor" / ".git").mkdir(parents=True)
    (root / "main.py").write_text("print('hi')\n")
    return root


def test_the_copies_folder_is_the_workspaces_in_the_state_directory(tmp_path: Path) -> None:
    assert copies_folder(tmp_path, "w-1") == tmp_path / "copies" / "w-1"


def test_a_copy_leaves_out_orbits_folder_and_git_at_its_top(
    workspace: Path, tmp_path: Path
) -> None:
    path = copy_directory(workspace, tmp_path / "copies")
    assert path.parent == tmp_path / "copies"
    assert sorted(p.name for p in path.iterdir()) == ["main.py", "vendor"]
    assert (path / "vendor" / ".git").is_dir()


def test_a_copy_whose_folder_cant_be_made_raises(workspace: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(TreeFailed, match=r"^Couldn't make a copy's directory in "):
        copy_directory(workspace, blocker / "copies")


def test_a_directory_that_cant_be_copied_raises_and_leaves_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    with pytest.raises(TreeFailed, match=r"^Couldn't copy "):
        copy_directory(tmp_path / "missing", folder)
    assert list(folder.iterdir()) == []


async def test_a_copy_is_set_up_and_removed_with_its_contents(
    workspace: Path, tmp_path: Path
) -> None:
    copies = DirectoryCopies(str(workspace), tmp_path / "copies", Setup(command=MARK))
    path = Path(await copies.make())
    assert (path / "set-up").read_text() == "yes"
    assert await copies.leftovers() == []
    await copies.remove(str(path))
    assert not path.exists()


async def test_a_copy_whose_setup_fails_raises_and_leaves_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "copies"
    copies = DirectoryCopies(str(workspace), folder, Setup(command=FAIL))
    with pytest.raises(TreeFailed, match=r"^Setting up the copy at .* failed: no network$"):
        await copies.make()
    assert list(folder.iterdir()) == []


async def test_the_copies_left_are_those_another_made(workspace: Path, tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    left = await DirectoryCopies(str(workspace), folder).make()
    (folder / "stray-file").write_text("")
    copies = DirectoryCopies(str(workspace), folder)
    await copies.make()
    assert await copies.leftovers() == [left]


async def test_no_folder_leaves_no_copies(tmp_path: Path) -> None:
    assert await DirectoryCopies(str(tmp_path), tmp_path / "copies").leftovers() == []


async def test_copies_that_cant_be_listed_raise(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    folder.mkdir()
    folder.chmod(0o000)
    try:
        with pytest.raises(TreeFailed, match=r"^Couldn't list the copies in "):
            await DirectoryCopies(str(tmp_path), folder).leftovers()
    finally:
        folder.chmod(0o755)


class HeldCopy:
    """Copying that sets :attr:`copying`, waits for :attr:`release`, then copies, or fails if
    ``fails``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fails: bool = False) -> None:
        self.copying, self.release = threading.Event(), threading.Event()
        self.fails = fails
        self._real = copy_directory
        monkeypatch.setattr(directory_copies, "copy_directory", self._copy)

    def _copy(self, root: Path, folder: Path) -> Path:
        self.copying.set()
        self.release.wait()
        if self.fails:
            raise TreeFailed("disk full")
        return self._real(root, folder)


@pytest.mark.parametrize("fails", [False, True])
async def test_a_copy_cancelled_while_copying_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, tmp_path: Path, fails: bool
) -> None:
    held = HeldCopy(monkeypatch, fails)
    folder = tmp_path / "copies"
    task = asyncio.create_task(DirectoryCopies(str(workspace), folder).make())
    await asyncio.to_thread(held.copying.wait)
    task.cancel()
    await asyncio.sleep(0)
    held.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not folder.exists() or list(folder.iterdir()) == []
