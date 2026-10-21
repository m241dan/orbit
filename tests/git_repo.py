"""Real throwaway git repositories for the tests that run git itself."""

import subprocess
from pathlib import Path

import pytest


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
