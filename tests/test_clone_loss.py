"""What deleting a clone would lose, as git sees it."""

from pathlib import Path

from orbit.agent.external.clone_loss import clone_loss
from tests.git_repo import bare_repository, commit_file, git


def cloned(tmp_path: Path) -> Path:
    """A clone, with its own git identity, of a bare repository with one commit."""
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(bare_repository(tmp_path)), str(clone))
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.com")
    return clone


async def test_a_clean_clone_would_lose_nothing(tmp_path: Path) -> None:
    assert await clone_loss(cloned(tmp_path)) == []


async def test_a_clone_that_is_gone_would_lose_nothing(tmp_path: Path) -> None:
    assert await clone_loss(tmp_path / "gone") == []


async def test_uncommitted_changes_and_stashes_would_be_lost(tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    (clone / "a.txt").write_text("two\n")
    git(clone, "stash", "-q")
    (clone / "a.txt").write_text("three\n")
    (clone / "b.txt").write_text("new\n")
    assert await clone_loss(clone) == ["uncommitted changes to 2 files", "1 stash"]


async def test_commits_not_pushed_are_named_by_their_branch(tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    git(clone, "switch", "-q", "-c", "fix")
    commit_file(clone, "b.txt", "new\n", "fix it")
    assert await clone_loss(clone) == ["1 commit on fix not on its remote"]
    git(clone, "push", "-q", "origin", "fix")
    assert await clone_loss(clone) == []


async def test_commits_on_a_detached_head_would_be_lost(tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    git(clone, "checkout", "-q", "--detach")
    commit_file(clone, "b.txt", "new\n", "one")
    commit_file(clone, "c.txt", "new\n", "two")
    assert await clone_loss(clone) == ["2 commits on a detached HEAD not on its remote"]


async def test_a_detached_head_at_a_pushed_commit_would_lose_nothing(tmp_path: Path) -> None:
    clone = cloned(tmp_path)
    git(clone, "checkout", "-q", "--detach")
    assert await clone_loss(clone) == []


async def test_an_empty_clone_would_lose_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "empty.git"
    git(tmp_path, "init", "-q", "--bare", str(empty))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(empty), str(clone))
    assert await clone_loss(clone) == []


async def test_a_clone_git_cant_read_may_lose_anything(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    [line] = await clone_loss(plain)
    assert line.startswith("whatever it holds, which can't be checked: ")
    assert "not a git repository" in line
