"""The repository's orbit directory, where an older orbit saved sessions, which the import
reads."""

from pathlib import Path

from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.repository import orbit_dir


def test_in_a_repository_sessions_lived_in_the_git_directory(repo: Path) -> None:
    assert orbit_dir(str(repo)) == repo / ".git" / "orbit"


def test_outside_a_repository_sessions_lived_in_the_workspace(tmp_path: Path) -> None:
    assert orbit_dir(str(tmp_path)) == tmp_path / ".orbit"


async def test_sessions_lived_beside_the_coders_worktree(repo: Path) -> None:
    coder = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    assert coder.parent == orbit_dir(str(repo))
