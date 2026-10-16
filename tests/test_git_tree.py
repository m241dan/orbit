"""The coder's worktree, against real throwaway git repositories."""

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.domain.roles import Setup
from orbit.agent.external import git_tree
from orbit.agent.external.git_tree import GitCoderTree, git_top_level, in_git_work_tree


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


def counting_setup(repo: Path, watch: tuple[str, ...] = ()) -> tuple[Setup, Path]:
    """A setup that appends an ``x`` to a file outside the repository each time it runs."""
    runs = repo.parent / "setup-runs"
    command = (sys.executable, "-c", f"open({str(runs)!r}, 'a').write('x')")
    return Setup(command=command, watch=watch), runs


async def test_setup_reruns_only_when_a_watched_file_changes(repo: Path) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    setup, runs = counting_setup(repo, watch=("uv.lock",))
    tree = GitCoderTree(str(repo), setup=setup)

    await tree.prepare(keep=False)
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"

    commit_file(repo, "a.txt", "two\n", "unwatched change")
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"

    commit_file(repo, "uv.lock", "v2\n", "lock changed")
    await tree.prepare(keep=False)
    assert runs.read_text() == "xx"


async def test_any_of_several_watched_files_changing_reruns_setup(repo: Path) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    setup, runs = counting_setup(repo, watch=("uv.lock", "deps/extra.txt"))
    tree = GitCoderTree(str(repo), setup=setup)
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"

    (repo / "deps").mkdir()
    commit_file(repo, "deps/extra.txt", "pin\n", "a watched file appears")
    await tree.prepare(keep=False)
    assert runs.read_text() == "xx"


async def test_setup_without_watched_files_runs_once(repo: Path) -> None:
    setup, runs = counting_setup(repo)
    tree = GitCoderTree(str(repo), setup=setup)
    await tree.prepare(keep=False)
    commit_file(repo, "uv.lock", "v1\n", "lock")
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"


async def test_a_changed_setup_command_runs_again(repo: Path) -> None:
    setup, runs = counting_setup(repo)
    await GitCoderTree(str(repo), setup=setup).prepare(keep=False)
    other = Setup(command=(*setup.command, "again"))
    await GitCoderTree(str(repo), setup=other).prepare(keep=False)
    assert runs.read_text() == "xx"


async def test_a_failing_setup_raises_and_runs_again_next_time(repo: Path) -> None:
    command = (sys.executable, "-c", "import sys; print('no network'); sys.exit(1)")
    tree = GitCoderTree(str(repo), setup=Setup(command=command))
    with pytest.raises(RuntimeError, match="no network"):
        await tree.prepare(keep=False)
    with pytest.raises(RuntimeError, match="no network"):
        await tree.prepare(keep=False)


async def test_setup_checks_its_files_off_the_event_loops_thread(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    setup, runs = counting_setup(repo, watch=("uv.lock",))
    threads: list[threading.Thread] = []
    real_fingerprint, real_read, real_write = (
        git_tree._fingerprint,
        Path.read_text,
        Path.write_text,
    )

    def fingerprint(setup: Setup, path: Path) -> str:
        threads.append(threading.current_thread())
        return real_fingerprint(setup, path)

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "coder.synced":
            threads.append(threading.current_thread())
        return real_read(self, *args, **kwargs)

    def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "coder.synced":
            threads.append(threading.current_thread())
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(git_tree, "_fingerprint", fingerprint)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "write_text", write_text)
    tree = GitCoderTree(str(repo), setup=setup)
    await tree.prepare(keep=False)
    await tree.prepare(keep=False)

    assert runs.read_text() == "x"
    assert len(threads) == 4  # Two fingerprints, one stamp written, then read.
    assert threading.current_thread() not in threads


async def test_without_setup_nothing_runs(repo: Path) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    assert not (path.parent / "coder.synced").exists()


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
    assert git_top_level(str(repo)) is None


def test_git_top_level_is_the_work_trees_root(repo: Path, tmp_path: Path) -> None:
    sub = repo / "sub" / "deeper"
    sub.mkdir(parents=True)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert git_top_level(str(sub)) == str(repo.resolve())
    assert git_top_level(str(repo)) == str(repo.resolve())
    assert git_top_level(str(outside)) is None


async def test_head_is_the_trees_latest_commit(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    assert await tree.head() == git(path, "rev-parse", "HEAD")


async def test_head_before_the_coder_tree_exists_raises_a_runtime_error(repo: Path) -> None:
    # git can't run in a directory that isn't there; callers expect a RuntimeError, not an OSError.
    tree = GitCoderTree(str(repo))
    with pytest.raises(RuntimeError, match="git rev-parse"):
        await tree.head()


async def test_base_commit_is_the_checkouts_head_before_the_tree_is_prepared(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    assert await tree.base_commit() == git(repo, "rev-parse", "HEAD")
    assert not (repo / ".git" / "orbit" / "coder").exists()


async def test_base_commit_follows_the_base_branch(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    commit_file(repo, "c.txt", "three\n", "user's commit")
    assert await tree.base_commit() == git(repo, "rev-parse", "main")
    assert await tree.base_commit() != git(path, "rev-parse", "HEAD")  # The tree lags behind.


async def test_base_commit_on_a_detached_checkout_is_its_head(repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    assert await tree.base_commit() == git(repo, "rev-parse", "HEAD")


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


async def test_a_snapshot_of_head_is_the_base_before_the_coder_tree_exists(repo: Path) -> None:
    # As the startup check takes it: orbit has only just started.
    tree = GitCoderTree(str(repo))
    snapshot = Path(await tree.snapshot("HEAD"))
    assert git(snapshot, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
    await tree.drop(str(snapshot))
    assert not snapshot.exists()


async def test_untracked_lists_new_files_with_new_directories_collapsed(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    assert await tree.untracked() == frozenset()
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "x.pyc").write_text("")
    (path / ".coverage").write_text("")
    assert await tree.untracked() == frozenset({"__pycache__/", ".coverage"})


async def test_ignored_paths_are_no_longer_committed_stashed_or_listed(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "x.pyc").write_text("")
    await tree.ignore({"__pycache__/"})
    await tree.ignore({"__pycache__/"})  # Ignoring again doesn't repeat it.

    assert await tree.untracked() == frozenset()
    assert not await tree.commit("nothing to commit")
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count("__pycache__/") == 1
    assert "orbit" in exclude


async def test_ignoring_paths_that_cant_be_written_raises_runtime_error(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    info = repo / ".git" / "info"
    (info / "exclude").unlink()
    info.rmdir()
    info.write_text("")  # Not a directory any more.
    with pytest.raises(RuntimeError, match="Couldn't ignore files in"):
        await tree.ignore({"__pycache__/"})


async def test_ignoring_paths_with_a_non_utf8_exclude_file_raises_runtime_error(
    repo: Path,
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_bytes(b"# \xff\n")
    with pytest.raises(RuntimeError, match=r"Couldn't ignore files in .*exclude"):
        await tree.ignore({"__pycache__/"})


async def test_a_setup_stamp_that_cant_be_read_raises_runtime_error(repo: Path) -> None:
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    (path.parent / "coder.synced").mkdir()  # A directory in the stamp's place.
    setup, runs = counting_setup(repo)
    with pytest.raises(RuntimeError, match=r"Couldn't read the setup record in .*coder\.synced"):
        await GitCoderTree(str(repo), setup=setup).prepare(keep=False)
    assert not runs.exists()


async def test_a_non_utf8_setup_stamp_raises_runtime_error(repo: Path) -> None:
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    (path.parent / "coder.synced").write_bytes(b"\xff")
    setup, _ = counting_setup(repo)
    with pytest.raises(RuntimeError, match=r"Couldn't read the setup record in .*coder\.synced"):
        await GitCoderTree(str(repo), setup=setup).prepare(keep=False)


async def test_a_watched_file_that_cant_be_read_raises_runtime_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    setup, runs = counting_setup(repo, watch=("uv.lock",))

    def read_bytes(self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(RuntimeError, match=r"Couldn't read .*uv\.lock to check the setup: denied"):
        await GitCoderTree(str(repo), setup=setup).prepare(keep=False)
    assert not runs.exists()


async def test_a_setup_that_cant_be_recorded_raises_runtime_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, _ = counting_setup(repo)
    real_write = Path.write_text

    def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "coder.synced":
            raise PermissionError("denied")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(RuntimeError, match=r"Couldn't record the setup in .*coder\.synced: denied"):
        await GitCoderTree(str(repo), setup=setup).prepare(keep=False)


async def test_a_setup_swapped_in_runs_at_the_next_prepare(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    setup, runs = counting_setup(repo)
    tree.use_setup(setup)
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"
    tree.use_setup(None)
    await tree.prepare(keep=False)
    assert runs.read_text() == "x"
