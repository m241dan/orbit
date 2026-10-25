"""Real throwaway git repositories for the tests that run git itself."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.waiting import eventually

_rmtree = shutil.rmtree


def git(cwd: Path, *args: str) -> str:
    """What git prints running ``args`` in ``cwd``, stripped; raises if it fails."""
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit_file(cwd: Path, name: str, text: str, message: str) -> None:
    """Write ``text`` to ``name`` in ``cwd`` and commit it with ``message``."""
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)


def init_repo(root: Path) -> Path:
    """``root``, made a repository on ``main`` with its own git identity."""
    root.mkdir(exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with its own git identity and one commit, of ``a.txt``."""
    root = init_repo(tmp_path / "repo")
    commit_file(root, "a.txt", "one\n", "start")
    return root


def bare_repository(tmp_path: Path, name: str = "proj.git") -> Path:
    """A bare repository ``name`` in ``tmp_path`` with one commit, of ``a.txt``."""
    source = init_repo(tmp_path / "source")
    commit_file(source, "a.txt", "one\n", "start")
    bare = tmp_path / name
    git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    return bare


def rmtree_stopping_partway(path: str | os.PathLike[str]) -> None:
    """``shutil.rmtree``, but for a repository: of that it deletes ``.git/HEAD`` and ``a.txt``,
    and then fails, as one a permission error stops partway would."""
    directory = Path(path)
    if not (directory / ".git").is_dir():
        _rmtree(directory)
        return
    (directory / ".git" / "HEAD").unlink()
    (directory / "a.txt").unlink()
    raise PermissionError("Permission denied")


async def started(pidfile: Path) -> int:
    """The pid the slow SSH noted, once it has."""
    await eventually(lambda: pidfile.exists() and pidfile.read_text().strip())
    return int(pidfile.read_text())
