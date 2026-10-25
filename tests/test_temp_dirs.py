"""The system's temporary directories agents working in none get: made empty, removed with what's
in them, and never left behind by a cancelled make."""

import asyncio
import tempfile
import threading
from pathlib import Path

import pytest

from orbit.agent.application.ports import TempDirFailed
from orbit.agent.external.temp_dirs import PREFIX, SystemTempDirs


@pytest.fixture
def temp_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The system's temporary directory, here a directory of the test's own."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


class HeldMkdtemp:
    """Making a temporary directory that sets :attr:`making`, waits for :attr:`release`, then
    makes one, or fails if :attr:`fails`, and sets :attr:`made`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fails: bool = False) -> None:
        self.making, self.release, self.made = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.fails = fails
        self._real = tempfile.mkdtemp
        monkeypatch.setattr(tempfile, "mkdtemp", self._mkdtemp)

    def _mkdtemp(self, prefix: str) -> str:
        self.making.set()
        self.release.wait()
        try:
            if self.fails:
                raise OSError("No space left on device")
            return self._real(prefix=prefix)
        finally:
            self.made.set()


async def test_a_temporary_directory_is_made_empty_and_removed_with_its_contents(
    temp_root: Path,
) -> None:
    temp_dirs = SystemTempDirs()
    path = Path(await temp_dirs.make())
    assert path.parent == temp_root and path.name.startswith(PREFIX)
    assert list(path.iterdir()) == []

    (path / "notes").mkdir()
    (path / "notes" / "a.txt").write_text("a")
    await temp_dirs.remove(str(path))
    assert not path.exists()


async def test_each_temporary_directory_is_new(temp_root: Path) -> None:
    temp_dirs = SystemTempDirs()
    assert await temp_dirs.make() != await temp_dirs.make()


async def test_removing_a_temporary_directory_thats_gone_does_nothing(temp_root: Path) -> None:
    await SystemTempDirs().remove(str(temp_root / "gone"))
    assert list(temp_root.iterdir()) == []


async def test_a_temporary_directory_that_cant_be_made_fails_saying_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "missing"))
    with pytest.raises(TempDirFailed, match=r"^Couldn't make a temporary directory: "):
        await SystemTempDirs().make()


@pytest.mark.parametrize("cancels", [1, 2])
async def test_a_make_cancelled_while_making_leaves_no_directory(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, cancels: int
) -> None:
    held = HeldMkdtemp(monkeypatch)
    task = asyncio.create_task(SystemTempDirs().make())
    await asyncio.to_thread(held.making.wait)
    for _ in range(cancels):  # Again while it waits for the thread: it still removes it.
        task.cancel()
        await asyncio.sleep(0)
    held.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert held.made.is_set()
    assert list(temp_root.iterdir()) == []


async def test_a_make_cancelled_while_failing_to_make_is_cancelled_all_the_same(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path
) -> None:
    held = HeldMkdtemp(monkeypatch, fails=True)
    task = asyncio.create_task(SystemTempDirs().make())
    await asyncio.to_thread(held.making.wait)
    task.cancel()
    await asyncio.sleep(0)
    held.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(temp_root.iterdir()) == []
