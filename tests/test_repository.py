"""Finding the repository a workspace is in, against real throwaway git repositories."""

import subprocess
from pathlib import Path

import pytest

from orbit.agent.external.repository import (
    git_common_dir,
    git_top_level,
    has_git_identity,
    in_git_work_tree,
    orbit_dir,
    workspace_fingerprint,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit_file(cwd: Path, name: str, text: str, message: str) -> None:
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    commit_file(root, "a.txt", "one\n", "start")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    return plain


def test_in_git_work_tree_tells_repositories_apart(repo: Path, outside: Path) -> None:
    assert in_git_work_tree(str(repo))
    assert not in_git_work_tree(str(outside))


def test_in_git_work_tree_is_false_inside_the_git_directory(repo: Path) -> None:
    assert not in_git_work_tree(str(repo / ".git"))


def test_without_git_nothing_is_found(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert not in_git_work_tree(str(repo))
    assert git_top_level(str(repo)) is None
    assert git_common_dir(str(repo)) is None
    assert not has_git_identity(str(repo))


@pytest.fixture
def no_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git configured with no identity beyond the repository's own config."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("EMAIL", "")
    for name in ("AUTHOR", "COMMITTER"):
        for part in ("NAME", "EMAIL"):
            monkeypatch.delenv(f"GIT_{name}_{part}", raising=False)


@pytest.mark.usefixtures("no_identity")
def test_a_repository_configured_with_an_identity_has_one(repo: Path) -> None:
    assert has_git_identity(str(repo))


@pytest.mark.usefixtures("no_identity")
def test_a_repository_without_user_email_has_no_identity(repo: Path) -> None:
    git(repo, "config", "--unset", "user.email")
    assert not has_git_identity(str(repo))


@pytest.mark.usefixtures("no_identity")
def test_an_identity_in_the_environment_counts(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.com")
    assert not has_git_identity(str(repo))
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.com")
    assert has_git_identity(str(repo))


def test_git_top_level_is_the_work_trees_root(repo: Path, outside: Path) -> None:
    sub = repo / "sub" / "deeper"
    sub.mkdir(parents=True)
    assert git_top_level(str(sub)) == str(repo.resolve())
    assert git_top_level(str(repo)) == str(repo.resolve())
    assert git_top_level(str(outside)) is None


def test_git_common_dir_is_the_repositorys_git_directory(repo: Path, outside: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    assert git_common_dir(str(sub)) == str(repo.resolve() / ".git")
    assert git_common_dir(str(outside)) is None


def test_git_common_dir_of_a_linked_worktree_is_the_main_repositorys(repo: Path) -> None:
    linked = repo.parent / "linked"
    git(repo, "worktree", "add", "-q", str(linked))
    assert git_common_dir(str(linked)) == str(repo.resolve() / ".git")


def test_a_repositorys_fingerprint_is_its_root_commit(repo: Path) -> None:
    root = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "b.txt", "two\n", "more")
    assert workspace_fingerprint(str(repo)) == f"{root} "


def test_a_repositorys_fingerprint_includes_its_origin(repo: Path) -> None:
    git(repo, "remote", "add", "origin", "https://example.com/repo.git")
    root = git(repo, "rev-parse", "HEAD")
    assert workspace_fingerprint(str(repo)) == f"{root} https://example.com/repo.git"


def test_there_is_no_fingerprint_outside_git_or_before_the_first_commit(
    tmp_path: Path, outside: Path
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q")
    assert workspace_fingerprint(str(outside)) is None
    assert workspace_fingerprint(str(fresh)) is None


def test_orbits_directory_is_in_the_repositorys_git_directory(repo: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    assert orbit_dir(str(sub)) == repo.resolve() / ".git" / "orbit"


def test_orbits_directory_from_a_linked_worktree_is_in_the_shared_git_directory(
    repo: Path,
) -> None:
    linked = repo.parent / "linked"
    git(repo, "worktree", "add", "-q", str(linked))
    assert orbit_dir(str(linked)) == repo.resolve() / ".git" / "orbit"


def test_orbits_directory_outside_a_repository_is_in_the_workspace(outside: Path) -> None:
    assert orbit_dir(str(outside)) == outside / ".orbit"


def test_orbits_directory_without_git_is_in_the_workspace(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert orbit_dir(str(repo)) == repo / ".orbit"
