"""The rules every coder tree keeps on the files commits changed, checked on the fake the other
tests use and on the real git tree alike, so that tests built on the fake hold for the real one."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbit.agent.application.ports import CoderTree
from orbit.agent.external.git_tree import GitCoderTree
from tests.agent_fakes import FakeTree
from tests.git_repo import commit_file, git

OLD = "src/x/external/auth.py"
NEW = "src/x/auth.py"


@dataclass(frozen=True)
class Subject:
    """A ``tree`` under test, and how to ``move`` a file in a commit of its own, which gives the
    commit before the move and the move's commit."""

    tree: CoderTree
    move: Callable[[str, str], Awaitable[tuple[str, str]]]


def fake_subject() -> Subject:
    tree = FakeTree()

    async def move(old: str, new: str) -> tuple[str, str]:
        tree.move("c0", old, new)
        return "c0", "c1"

    return Subject(tree, move)


def git_subject(repo: Path) -> Subject:
    async def move(old: str, new: str) -> tuple[str, str]:
        (repo / old).parent.mkdir(parents=True, exist_ok=True)
        commit_file(repo, old, "secret\n", "Add the file")
        start = git(repo, "rev-parse", "HEAD")
        git(repo, "mv", old, new)
        git(repo, "commit", "-q", "-m", "Move the file")
        return start, git(repo, "rev-parse", "HEAD")

    return Subject(GitCoderTree(str(repo)), move)


@pytest.fixture(params=["fake", "git"])
def subject(request: pytest.FixtureRequest, repo: Path) -> Subject:
    """Each coder tree in turn: the fake, and a real one over the throwaway ``repo``."""
    if request.param == "fake":
        return fake_subject()
    return git_subject(repo)


async def test_a_moved_file_changed_both_its_old_and_its_new_path(subject: Subject) -> None:
    start, end = await subject.move(OLD, NEW)
    assert sorted(await subject.tree.changed_files(start, end)) == [NEW, OLD]
