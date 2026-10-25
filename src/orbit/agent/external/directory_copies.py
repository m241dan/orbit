"""The copies agents whose role works in a copy work in, outside git: copies of the workspace's
directory, without orbit's project folder or a git directory, made in orbit's state directory
and set up there."""

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from orbit.agent.application.ports import TreeFailed
from orbit.agent.domain.checks import Setup
from orbit.agent.external.git_tree import run_setup
from orbit.agent.external.shielding import shielded

COPIES_DIR = "copies"
"""The folder in the state directory the copies are made in, a folder for each workspace."""

PREFIX = "copy-"
"""What the names of the copies start with."""

LEFT_OUT = (".orbit", ".git")
"""What a copy leaves out of the workspace's directory: orbit's project folder and git's."""


def copies_folder(state_dir: Path, workspace_id: str) -> Path:
    """The folder the copies of the workspace ``workspace_id`` are made in, in ``state_dir``."""
    return state_dir / COPIES_DIR / workspace_id


def copy_directory(root: Path, folder: Path) -> Path:
    """A new copy of the directory ``root`` in ``folder``, without :data:`LEFT_OUT` at its top,
    named so it can't collide with any other; returns its path.

    Raises ``TreeFailed`` when it can't be made, leaving nothing of it behind.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=PREFIX, dir=folder))
    except OSError as error:
        raise TreeFailed(f"Couldn't make a copy's directory in {folder}: {error}") from error
    try:
        shutil.copytree(root, path, symlinks=True, ignore=_top_level(root), dirs_exist_ok=True)
    except (OSError, shutil.Error) as error:
        shutil.rmtree(path, ignore_errors=True)
        raise TreeFailed(f"Couldn't copy {root} to {path}: {error}") from error
    return path


def _top_level(root: Path) -> Callable[[str, list[str]], set[str]]:
    """What leaves :data:`LEFT_OUT` out of the top of ``root`` as it's copied."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        return set(LEFT_OUT) & set(names) if Path(directory) == root else set()

    return ignore


def _copies_in(folder: Path) -> set[Path]:
    """The copies in ``folder``; none when it's missing.

    Raises ``TreeFailed`` when it can't be listed.
    """
    try:
        if not folder.is_dir():
            return set()
        return {path for path in folder.iterdir() if path.is_dir() and path.name.startswith(PREFIX)}
    except OSError as error:
        raise TreeFailed(f"Couldn't list the copies in {folder}: {error}") from error


class DirectoryCopies:
    """Copies of the workspace's directory at ``root``, made in ``folder`` and set up there with
    ``setup``, if there is one."""

    def __init__(self, root: str, folder: Path, setup: Setup | None = None) -> None:
        self._root = Path(root)
        self._folder = folder
        self._setup = setup
        self._made: set[Path] = set()

    async def make(self) -> str:
        making = asyncio.ensure_future(asyncio.to_thread(copy_directory, self._root, self._folder))
        try:
            path = await shielded(making)
        except asyncio.CancelledError:
            with contextlib.suppress(TreeFailed):  # It failed to make one: nothing to remove.
                await asyncio.to_thread(shutil.rmtree, making.result(), ignore_errors=True)
            raise
        self._made.add(path)
        try:
            await self._set_up(path)
        except BaseException:  # Cancellation too: the caller never gets the path to remove.
            await self.remove(str(path))
            raise
        return str(path)

    async def remove(self, path: str) -> None:
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        self._made.discard(Path(path))

    async def leftovers(self) -> list[str]:
        found = await asyncio.to_thread(_copies_in, self._folder)
        return sorted(str(path) for path in found - self._made)

    async def _set_up(self, path: Path) -> None:
        """Run the setup, if there is one, in the copy at ``path``."""
        if self._setup is None:
            return
        ok, output = await run_setup(self._setup.command, path)
        if not ok:
            raise TreeFailed(f"Setting up the copy at {path} failed: {output}")
