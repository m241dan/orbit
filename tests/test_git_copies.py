"""The coder tree's copies, against real throwaway git repositories: writable detached trees of a
commit under ``.git/orbit/copies``, set up as the tree is, removed when dropped."""

import shutil
import sys
from pathlib import Path

import pytest

from orbit.agent.application.ports import TreeFailed
from orbit.agent.domain.checks import Setup
from orbit.agent.external.git_tree import GitCoderTree
from tests.git_repo import commit_file, git

MARK = (sys.executable, "-c", "open('set-up', 'w').write('yes')")
"""A setup that marks the directory it runs in as set up."""

FAIL = (sys.executable, "-c", "import sys; print('no network'); sys.exit(1)")
"""A setup that fails."""


def copies_dir(repo: Path) -> Path:
    return repo / ".git" / "orbit" / "copies"


async def test_a_copy_is_a_writable_set_up_tree_of_the_commit_under_git(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "a.txt", "two\n", "later")
    tree = GitCoderTree(str(repo), Setup(command=MARK))
    path = Path(await tree.copy(base))
    assert path.parent == copies_dir(repo)
    assert (path / "a.txt").read_text() == "one\n"
    assert (path / "set-up").read_text() == "yes"
    (path / "notes.txt").write_text("found\n")
    assert git(repo, "status", "--porcelain") == ""  # The user's checkout is untouched.


async def test_a_dropped_copy_is_gone_with_what_was_written_in_it(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.copy("HEAD"))
    (path / "notes.txt").write_text("found\n")
    await tree.drop_copy(str(path))
    assert not path.exists()
    assert str(path) not in git(repo, "worktree", "list")


async def test_dropping_a_copy_deleted_by_hand_unregisters_it(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.copy("HEAD"))
    shutil.rmtree(path)
    await tree.drop_copy(str(path))
    assert str(path) not in git(repo, "worktree", "list")


async def test_a_copy_whose_setup_fails_raises_and_leaves_nothing(repo: Path) -> None:
    tree = GitCoderTree(str(repo), Setup(command=FAIL))
    with pytest.raises(TreeFailed, match=r"^Setting up the copy at .* failed: no network$"):
        await tree.copy("HEAD")
    assert list(copies_dir(repo).iterdir()) == []


async def test_the_copies_listed_are_those_the_tree_didnt_make(repo: Path) -> None:
    earlier = GitCoderTree(str(repo))
    left = await earlier.copy("HEAD")
    tree = GitCoderTree(str(repo))
    await tree.copy("HEAD")
    assert await tree.copies() == [left]


async def test_no_copies_folder_lists_no_copies(repo: Path) -> None:
    assert await GitCoderTree(str(repo)).copies() == []


async def test_copies_that_cant_be_listed_raise(repo: Path) -> None:
    folder = copies_dir(repo)
    folder.mkdir(parents=True)
    folder.chmod(0o000)
    try:
        with pytest.raises(TreeFailed, match=r"Couldn't list the copies"):
            await GitCoderTree(str(repo)).copies()
    finally:
        folder.chmod(0o755)
