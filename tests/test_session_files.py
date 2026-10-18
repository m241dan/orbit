"""Where an older orbit saved sessions on disk, which the import reads."""

import subprocess
from pathlib import Path

import pytest

from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.session_files import state_dir


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    return root


def test_in_a_repository_sessions_lived_in_the_git_directory(repo: Path) -> None:
    assert state_dir(str(repo)) == repo / ".git" / "orbit"


def test_outside_a_repository_sessions_lived_in_the_workspace(tmp_path: Path) -> None:
    assert state_dir(str(tmp_path)) == tmp_path / ".orbit"


async def test_sessions_lived_beside_the_coders_worktree(repo: Path) -> None:
    git(
        repo,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    coder = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    assert coder.parent == state_dir(str(repo))
