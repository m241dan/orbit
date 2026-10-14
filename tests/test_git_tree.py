"""The coder's worktree, against real throwaway git repositories."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from orbit.agent.external.git_tree import GitCoderTree, in_git_work_tree


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


def log(cwd: Path, ref: str = "HEAD") -> list[str]:
    return git(cwd, "log", "--format=%s", ref).splitlines()


async def test_prepare_creates_the_coder_worktree_inside_git(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))

    assert path == repo / ".git" / "orbit" / "coder"
    assert (path / "a.txt").read_text() == "one\n"
    assert git(path, "branch", "--show-current") == "orbit/coder"
    assert git(repo, "status", "--porcelain") == ""  # Invisible to the main checkout.


async def test_prepare_reuses_the_worktree(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    first = await tree.prepare(keep=False)
    (Path(first) / "marker").write_text("x")
    git(Path(first), "add", "marker")
    git(Path(first), "commit", "-q", "-m", "marker")
    assert await tree.prepare(keep=False) == first
    assert (Path(first) / "marker").exists()


async def test_prepare_recreates_a_worktree_deleted_behind_its_back(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    subprocess.run(["rm", "-rf", str(path)], check=True)
    assert await tree.prepare(keep=False) == str(path)
    assert (path / "a.txt").exists()


async def test_leftover_changes_are_stashed_unless_kept(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("half done\n")
    (path / "new.txt").write_text("new\n")

    await tree.prepare(keep=True)
    assert (path / "a.txt").read_text() == "half done\n"

    await tree.prepare(keep=False)
    assert (path / "a.txt").read_text() == "one\n"
    assert not (path / "new.txt").exists()
    assert "orbit: unverified work" in git(repo, "stash", "list")


async def test_commit_records_the_work_or_reports_there_was_none(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    assert not await tree.commit("nothing")

    (path / "b.txt").write_text("two\n")
    assert await tree.commit("Add b\n\nadded the b file")
    assert log(path)[0] == "Add b"
    assert git(path, "status", "--porcelain") == ""


async def test_a_failing_commit_raises_with_gits_message(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    with pytest.raises(RuntimeError, match="hook says no"):
        await tree.commit("Add b")


async def test_integrate_fast_forwards_the_checked_out_base(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")

    merge = await tree.integrate()
    assert merge.result == "merged"
    assert log(repo)[0] == "Add b"
    assert (repo / "b.txt").read_text() == "two\n"


async def test_integrate_with_nothing_new_is_merged(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    assert (await tree.integrate()).result == "merged"


async def test_a_moved_base_is_rebased_onto_first_then_merged(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    commit_file(repo, "c.txt", "three\n", "user's commit")

    assert (await tree.integrate()).result == "rebased"
    assert log(path)[:2] == ["Add b", "user's commit"]
    assert log(repo)[0] == "user's commit"  # Not merged until verified again.

    assert (await tree.integrate()).result == "merged"
    assert log(repo)[:2] == ["Add b", "user's commit"]


async def test_a_conflicting_base_is_left_alone(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    commit_file(repo, "a.txt", "user\n", "user edits a")

    merge = await tree.integrate()
    assert merge.result == "conflict"
    assert "a.txt" in merge.detail
    assert log(path)[0] == "Orbit edits a"
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert git(path, "status", "--porcelain") == ""


async def test_a_checkout_with_changes_in_the_way_blocks_the_merge(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    (repo / "a.txt").write_text("user's unsaved edit\n")

    merge = await tree.integrate()
    assert merge.result == "blocked"
    assert merge.detail
    assert (repo / "a.txt").read_text() == "user's unsaved edit\n"
    assert log(repo)[0] == "start"

    git(repo, "checkout", "--", "a.txt")
    assert (await tree.integrate()).result == "merged"


async def test_a_base_no_longer_checked_out_is_moved_without_touching_the_checkout(
    repo: Path,
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    git(repo, "checkout", "-q", "-b", "elsewhere")
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")

    assert (await tree.integrate()).result == "merged"
    assert log(repo, "main")[0] == "Add b"
    assert not (repo / "b.txt").exists()


async def test_a_detached_checkout_has_no_base_to_merge_into(repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    merge = await tree.integrate()
    assert merge.result == "detached"


async def test_prepare_catches_up_with_a_base_that_moved_ahead(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    commit_file(repo, "c.txt", "three\n", "user's commit")
    await tree.prepare(keep=False)
    assert log(path)[0] == "user's commit"


async def test_prepare_rebases_unmerged_work_onto_a_moved_base(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    commit_file(repo, "c.txt", "three\n", "user's commit")
    await tree.prepare(keep=False)
    assert log(path)[:2] == ["Add b", "user's commit"]


async def test_prepare_leaves_unmerged_work_that_conflicts_with_the_base(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    await tree.prepare(keep=False)
    assert log(path)[:2] == ["Orbit edits a", "start"]
    assert git(path, "status", "--porcelain") == ""


async def test_setup_runs_when_the_lock_file_changes(repo: Path) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    runs = repo.parent / "setup-runs"
    setup = (sys.executable, "-c", f"open({str(runs)!r}, 'a').write('x')")
    tree = GitCoderTree(str(repo), setup=setup)

    await tree.prepare(keep=False)
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"

    commit_file(repo, "uv.lock", "v2\n", "lock changed")
    await tree.prepare(keep=False)
    assert runs.read_text() == "xx"


async def test_a_failing_setup_raises(repo: Path) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    setup = (sys.executable, "-c", "import sys; print('no network'); sys.exit(1)")
    tree = GitCoderTree(str(repo), setup=setup)
    with pytest.raises(RuntimeError, match="no network"):
        await tree.prepare(keep=False)


async def test_no_lock_file_means_no_setup(repo: Path) -> None:
    runs = repo.parent / "setup-runs"
    setup = (sys.executable, "-c", f"open({str(runs)!r}, 'a').write('x')")
    await GitCoderTree(str(repo), setup=setup).prepare(keep=False)
    assert not runs.exists()


async def test_leftovers_report_unmerged_commits_and_stashed_work(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    assert await tree.leftovers() == []
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    (repo / "a.txt").write_text("in the way\n")
    await tree.integrate()
    (path / "b.txt").write_text("half done\n")
    await tree.prepare(keep=False)

    notes = await tree.leftovers()
    assert len(notes) == 2
    assert "1 commit" in notes[0] and "orbit/coder" in notes[0]
    assert "stash" in notes[1]


def test_outside_a_repository_prepare_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git"):
        asyncio.run(GitCoderTree(str(tmp_path)).prepare(keep=False))


async def test_no_leftovers_once_everything_is_merged(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    await tree.integrate()
    assert await tree.leftovers() == []


def test_in_git_work_tree_tells_repositories_apart(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    assert in_git_work_tree(str(repo))
    assert not in_git_work_tree(str(outside))


def test_without_git_nothing_is_a_work_tree(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert not in_git_work_tree(str(repo))


async def test_head_is_the_trees_latest_commit(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    assert await tree.head() == git(path, "rev-parse", "HEAD")


async def test_show_gives_each_commits_message_and_diff(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    first = await tree.head()
    (path / "c.txt").write_text("three\n")
    await tree.commit("Add c")
    shown = await tree.show([first, await tree.head()])
    assert shown.index("Add b") < shown.index("+two") < shown.index("Add c") < shown.index("+three")


async def test_a_snapshot_is_a_detached_tree_at_the_commit_until_dropped(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    commit = await tree.head()
    (path / "c.txt").write_text("three\n")
    await tree.commit("Add c")

    snapshot = Path(await tree.snapshot(commit))
    assert snapshot.parent == repo / ".git" / "orbit"
    assert (snapshot / "b.txt").exists() and not (snapshot / "c.txt").exists()
    assert git(snapshot, "rev-parse", "HEAD") == commit
    assert git(snapshot, "branch", "--show-current") == ""

    await tree.drop(str(snapshot))
    assert not snapshot.exists()
    assert str(snapshot) not in git(repo, "worktree", "list")


async def test_two_snapshots_of_one_commit_are_separate(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    commit = await tree.head()
    first, second = await tree.snapshot(commit), await tree.snapshot(commit)
    assert first != second
