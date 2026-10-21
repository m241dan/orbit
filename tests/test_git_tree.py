"""The coder's worktree, against real throwaway git repositories."""

import asyncio
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.domain.checks import Setup
from orbit.agent.external import git_tree
from orbit.agent.external.git_tree import GitCoderTree


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


async def test_the_tree_holds_work_while_it_has_changes_or_commits_the_base_lacks(
    repo: Path,
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    assert not await tree.holds_work()

    (path / "b.txt").write_text("two\n")
    assert await tree.holds_work()
    await tree.commit("Add b")
    assert await tree.holds_work()

    await tree.integrate()
    assert not await tree.holds_work()


async def test_a_tree_behind_a_base_that_moved_on_holds_no_work(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    commit_file(repo, "c.txt", "three\n", "user's commit")
    assert not await tree.holds_work()


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


async def test_a_rebase_failing_without_conflicts_raises_with_gits_message(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    commit_file(repo, "c.txt", "three\n", "user's commit")
    (path / "c.txt").write_text("in the way\n")

    with pytest.raises(RuntimeError, match=r"(?s)git rebase failed: .*c\.txt") as raised:
        await tree.integrate()
    assert "no rebase in progress" not in str(raised.value)
    assert log(path)[0] == "Add b"
    assert (path / "c.txt").read_text() == "in the way\n"


async def test_a_rebase_is_aborted_even_when_listing_its_conflicts_fails(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    real = GitCoderTree._git

    async def failing_diff(self: GitCoderTree, cwd: Path, *args: str) -> str:
        if args[0] == "diff":
            raise RuntimeError("git diff failed: out of memory")
        return await real(self, cwd, *args)

    monkeypatch.setattr(GitCoderTree, "_git", failing_diff)
    with pytest.raises(RuntimeError, match="out of memory"):
        await tree.integrate()
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert log(path)[0] == "Orbit edits a"


async def test_prepare_aborts_a_rebase_an_earlier_run_left_in_progress(repo: Path) -> None:
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    git(path, "commit", "-q", "-am", "Orbit edits a")
    before = git(path, "rev-parse", "HEAD")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    interrupted = subprocess.run(["git", "rebase", "-q", "main"], cwd=path, capture_output=True)
    assert interrupted.returncode != 0
    assert (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()

    assert Path(await GitCoderTree(str(repo)).prepare(keep=False)) == path
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert git(path, "branch", "--show-current") == "orbit/coder"
    assert git(path, "rev-parse", "HEAD") == before
    assert git(path, "status", "--porcelain") == ""
    assert git(repo, "stash", "list") == ""


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
    assert len(threads) == 5  # Two fingerprints, two stamp reads (the first finds none), one write.
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


async def test_a_snapshot_is_named_for_its_commit_not_the_ref(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    snapshot = Path(await tree.snapshot("HEAD"))
    assert snapshot.name.startswith(f"snapshot-{git(repo, 'rev-parse', 'HEAD')[:12]}-")
    await tree.drop(str(snapshot))


async def test_a_snapshot_of_a_missing_commit_fails_and_leaves_nothing(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    with pytest.raises(RuntimeError, match="git rev-parse"):
        await tree.snapshot("no-such-commit")
    assert await tree.leftovers() == []


def snapshot_dirs(repo: Path) -> list[Path]:
    return list((repo / ".git" / "orbit").glob("snapshot-*"))


def failing_add(monkeypatch: pytest.MonkeyPatch, *, after: bool, lock: bool = False) -> None:
    """Make ``git worktree add`` raise, before running or ``after`` it has made the tree,
    which it can ``lock`` first, as git leaves a half-made one."""
    real = GitCoderTree._git

    async def fake(self: GitCoderTree, cwd: Path, *args: str) -> str:
        if args[:2] != ("worktree", "add"):
            return await real(self, cwd, *args)
        if after:
            await real(self, cwd, *args)
        if lock:
            await real(self, cwd, "worktree", "lock", args[-2])
        raise RuntimeError("git worktree failed: index.lock exists")

    monkeypatch.setattr(GitCoderTree, "_git", fake)


@pytest.mark.parametrize("after", [False, True])
async def test_a_failed_snapshot_leaves_no_directory_or_worktree(
    monkeypatch: pytest.MonkeyPatch, repo: Path, after: bool
) -> None:
    failing_add(monkeypatch, after=after)
    tree = GitCoderTree(str(repo))
    with pytest.raises(RuntimeError, match=r"index\.lock"):
        await tree.snapshot("HEAD")
    assert snapshot_dirs(repo) == []
    assert "snapshot-" not in git(repo, "worktree", "list")
    assert await GitCoderTree(str(repo)).leftovers() == []


async def test_a_failed_snapshot_left_locked_is_unregistered(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    failing_add(monkeypatch, after=True, lock=True)
    tree = GitCoderTree(str(repo))
    with pytest.raises(RuntimeError, match=r"index\.lock"):
        await tree.snapshot("HEAD")
    assert snapshot_dirs(repo) == []
    assert "snapshot-" not in git(repo, "worktree", "list", "--porcelain")
    assert list((repo / ".git" / "worktrees").glob("snapshot-*")) == []
    assert await GitCoderTree(str(repo)).leftovers() == []


async def test_a_failing_cleanup_doesnt_hide_why_the_snapshot_failed(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    failing_add(monkeypatch, after=False)
    real_run = git_tree._run

    async def no_remove(command: Sequence[str], cwd: Path) -> tuple[bool, str]:
        if tuple(command[1:3]) == ("worktree", "remove"):
            raise RuntimeError("git worktree failed to start")
        return await real_run(command, cwd)

    monkeypatch.setattr(git_tree, "_run", no_remove)
    tree = GitCoderTree(str(repo))
    with pytest.raises(RuntimeError, match=r"index\.lock"):
        await tree.snapshot("HEAD")
    assert snapshot_dirs(repo) == [] and tree._snapshots == set()


async def test_an_unremovable_snapshot_directory_doesnt_hide_why_the_snapshot_failed(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    real = GitCoderTree._git

    async def locking_add(self: GitCoderTree, cwd: Path, *args: str) -> str:
        if args[:2] == ("worktree", "add"):
            Path(args[-2]).parent.chmod(0o555)
            raise RuntimeError("git worktree failed: index.lock exists")
        return await real(self, cwd, *args)

    monkeypatch.setattr(GitCoderTree, "_git", locking_add)
    tree = GitCoderTree(str(repo))
    try:
        with pytest.raises(RuntimeError, match=r"index\.lock"):
            await tree.snapshot("HEAD")
    finally:
        (repo / ".git" / "orbit").chmod(0o755)
    assert len(snapshot_dirs(repo)) == 1 and tree._snapshots == set()


async def test_a_snapshot_cancelled_while_being_added_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    real = GitCoderTree._git
    adding = asyncio.Event()

    async def blocking(self: GitCoderTree, cwd: Path, *args: str) -> str:
        if args[:2] == ("worktree", "add"):
            adding.set()
            await asyncio.Event().wait()
        return await real(self, cwd, *args)

    monkeypatch.setattr(GitCoderTree, "_git", blocking)
    tree = GitCoderTree(str(repo))
    task = asyncio.create_task(tree.snapshot("HEAD"))
    await adding.wait()
    assert len(snapshot_dirs(repo)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert snapshot_dirs(repo) == [] and tree._snapshots == set()


async def test_a_snapshot_cancelled_while_making_its_directory_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    real = git_tree._make_snapshot_dir
    making, release, made = threading.Event(), threading.Event(), threading.Event()

    def blocking(parent: Path, commit: str) -> Path:
        making.set()
        release.wait()
        path = real(parent, commit)
        made.set()
        return path

    monkeypatch.setattr(git_tree, "_make_snapshot_dir", blocking)
    tree = GitCoderTree(str(repo))
    task = asyncio.create_task(tree.snapshot("HEAD"))
    await asyncio.to_thread(making.wait)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(made.wait)
    assert snapshot_dirs(repo) == [] and tree._snapshots == set()


@pytest.mark.parametrize("worktree", [True, False])
async def test_a_leftover_from_the_old_naming_doesnt_block_a_snapshot(
    repo: Path, worktree: bool
) -> None:
    old = repo / ".git" / "orbit" / "review-HEAD-1"
    if worktree:
        git(repo, "worktree", "add", "-q", "--detach", str(old), "HEAD")
    else:
        old.mkdir(parents=True)
    tree = GitCoderTree(str(repo))
    snapshot = Path(await tree.snapshot("HEAD"))
    assert snapshot != old and (snapshot / "a.txt").exists()


async def test_two_sessions_can_snapshot_one_commit_at_once(repo: Path) -> None:
    first, second = GitCoderTree(str(repo)), GitCoderTree(str(repo))
    paths = await asyncio.gather(first.snapshot("HEAD"), second.snapshot("HEAD"))
    assert paths[0] != paths[1]
    assert all((Path(path) / "a.txt").exists() for path in paths)


async def test_snapshots_left_by_an_earlier_run_dont_block_a_later_one(repo: Path) -> None:
    earlier = GitCoderTree(str(repo))
    await earlier.prepare(keep=False)
    commit = await earlier.head()
    left = {await earlier.snapshot(commit), await earlier.snapshot("HEAD")}  # Never dropped.

    later = GitCoderTree(str(repo))
    taken = [await later.snapshot(commit), await later.snapshot("HEAD")]
    assert len(set(taken)) == 2 and not left & set(taken)
    for path in taken:
        await later.drop(path)
        assert not Path(path).exists()
    assert all(Path(path).exists() for path in left)


async def test_leftovers_report_snapshots_left_by_other_sessions_only(repo: Path) -> None:
    earlier, tree = GitCoderTree(str(repo)), GitCoderTree(str(repo))
    await earlier.snapshot("HEAD")
    await earlier.snapshot("HEAD")
    own = await tree.snapshot("HEAD")

    assert await tree.leftovers() == [
        "2 snapshot worktree(s) left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    ]
    assert await earlier.leftovers() == [
        "1 snapshot worktree(s) left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    ]
    await tree.drop(own)
    assert await earlier.leftovers() == []  # Dropped, so no longer there.


async def test_leftovers_report_snapshots_of_the_old_naming(repo: Path) -> None:
    git(repo, "worktree", "add", "-q", "--detach", str(repo / ".git" / "orbit" / "review-HEAD-1"))
    notes = await GitCoderTree(str(repo)).leftovers()
    assert notes == [
        "1 snapshot worktree(s) left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    ]


async def test_the_coder_tree_is_not_a_leftover_snapshot(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    assert await GitCoderTree(str(repo)).leftovers() == []


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


async def test_a_setup_stamp_that_vanishes_while_read_runs_the_setup(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    stamp = path.parent / "coder.synced"
    stamp.write_text("stale")
    real_read = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == stamp:
            self.unlink(missing_ok=True)  # Gone after any check that it was there.
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    setup, runs = counting_setup(repo)
    await GitCoderTree(str(repo), setup=setup).prepare(keep=False)
    assert runs.read_text() == "x"


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
