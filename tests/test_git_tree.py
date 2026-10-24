"""The coder's worktree, against real throwaway git repositories."""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.application.ports import TreeFailed
from orbit.agent.domain.checks import Setup
from orbit.agent.external import git_tree, whole_files
from orbit.agent.external.git import GitResult, GitUnavailable
from orbit.agent.external.git import git as run_git
from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.repository import find_git_common_dir
from tests.git_repo import commit_file, git


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
    with pytest.raises(TreeFailed, match="hook says no"):
        await tree.commit("Add b")


async def test_commit_raises_when_git_cant_tell_whats_staged(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    commit_file(path, "b.txt", "two\n", "Add b")
    before = git(path, "rev-parse", "HEAD")
    tree_id = git(path, "rev-parse", "HEAD^{tree}")
    (repo / ".git" / "objects" / tree_id[:2] / tree_id[2:]).unlink()  # HEAD can't be read.
    (path / "c.txt").write_text("three\n")

    with pytest.raises(TreeFailed, match=r"git diff failed: .*bad tree object"):
        await tree.commit("Add c")
    assert git(path, "rev-parse", "HEAD") == before


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

    with pytest.raises(TreeFailed, match=r"(?s)git rebase failed: .*c\.txt") as raised:
        await tree.integrate()
    assert "no rebase in progress" not in str(raised.value)
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert log(path)[0] == "Add b"
    assert (path / "c.txt").read_text() == "in the way\n"


async def test_a_rebase_refused_before_it_starts_raises_with_gits_message(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    before = git(path, "rev-parse", "HEAD")
    commit_file(repo, "c.txt", "three\n", "user's commit")
    hook = repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text("#!/bin/sh\necho refused by the hook\nexit 1\n")
    hook.chmod(0o755)

    refused = r"(?s)git rebase failed: .*refused by the hook"
    with pytest.raises(TreeFailed, match=refused) as raised:
        await tree.integrate()
    assert "no rebase in progress" not in str(raised.value).lower()
    assert git(path, "rev-parse", "HEAD") == before
    assert git(path, "branch", "--show-current") == "orbit/coder"


async def test_a_rebase_is_aborted_even_when_listing_its_conflicts_fails(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    before = git(path, "rev-parse", "HEAD")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    real = GitCoderTree._git

    async def failing_diff(self: GitCoderTree, cwd: Path, *args: str) -> str:
        if args[0] == "diff":
            raise TreeFailed("git diff failed: out of memory")
        return await real(self, cwd, *args)

    monkeypatch.setattr(GitCoderTree, "_git", failing_diff)
    with pytest.raises(TreeFailed, match="out of memory"):
        await tree.integrate()
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert git(path, "rev-parse", "HEAD") == before
    assert git(path, "branch", "--show-current") == "orbit/coder"


async def test_a_rebase_cancelled_while_running_is_aborted(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    before = git(path, "rev-parse", "HEAD")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    real = run_git
    rebasing = asyncio.Event()

    async def hanging_rebase(cwd: Path | str, *args: str) -> GitResult:
        result = await real(cwd, *args)
        if args[0] == "rebase" and "--abort" not in args:
            rebasing.set()  # Stopped at its conflict, as if still running when cancelled.
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(git_tree, "git", hanging_rebase)
    task = asyncio.create_task(tree.integrate())
    await rebasing.wait()
    assert (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert git(path, "rev-parse", "HEAD") == before
    assert git(path, "status", "--porcelain") == ""


async def test_a_rebase_cancelled_while_handling_its_conflict_is_still_aborted(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    before = git(path, "rev-parse", "HEAD")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    real = run_git
    listing, aborting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_conflict_handling(cwd: Path | str, *args: str) -> GitResult:
        if args[0] == "diff":
            listing.set()
            await asyncio.Event().wait()
        if args == ("rebase", "--abort"):
            aborting.set()
            await release.wait()
        return await real(cwd, *args)

    monkeypatch.setattr(git_tree, "git", slow_conflict_handling)
    task = asyncio.create_task(tree.integrate())
    await listing.wait()
    task.cancel()
    await aborting.wait()
    task.cancel()  # Again, while it aborts: the abort still finishes.
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (repo / ".git" / "worktrees" / "coder" / "rebase-merge").exists()
    assert git(path, "rev-parse", "HEAD") == before
    assert git(path, "status", "--porcelain") == ""


async def test_a_failing_abort_doesnt_hide_a_cancelled_rebase(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    rebasing = asyncio.Event()

    async def failing_abort(cwd: Path | str, *args: str) -> GitResult:
        if args == ("rebase", "--abort"):
            return GitResult(128, b"abort failed")
        if args[0] == "rebase":
            rebasing.set()
            await asyncio.Event().wait()
        return await run_git(cwd, *args)

    monkeypatch.setattr(git_tree, "git", failing_abort)
    task = asyncio.create_task(tree.integrate())
    await rebasing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_failing_abort_doesnt_hide_why_the_rebase_failed(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    commit_file(repo, "a.txt", "user\n", "user edits a")
    real = run_git

    async def failing(cwd: Path | str, *args: str) -> GitResult:
        if args[0] == "diff":
            return GitResult(128, b"out of memory")
        if args == ("rebase", "--abort"):
            return GitResult(128, b"abort failed too")
        return await real(cwd, *args)

    monkeypatch.setattr(git_tree, "git", failing)
    with pytest.raises(TreeFailed, match="out of memory"):
        await tree.integrate()


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
    assert log(path)[:2] == ["Orbit edits a", "start"]
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


async def test_a_git_failure_reading_the_checked_out_branch_fails_the_merge_and_moves_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    ran = failing_git(monkeypatch, "symbolic-ref")
    with pytest.raises(TreeFailed, match="git symbolic-ref failed: fatal: bad object deadbeef"):
        await tree.integrate()
    assert not [args for args in ran if args[0] in ("merge", "update-ref")]
    assert log(repo, "main")[0] == "start"
    assert git(repo, "status", "--porcelain") == ""


async def test_a_git_failure_reading_the_checked_out_branch_fails_prepare(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    failing_git(monkeypatch, "symbolic-ref")
    tree = GitCoderTree(str(repo))
    with pytest.raises(TreeFailed, match="git symbolic-ref failed: fatal: bad object deadbeef"):
        await tree.prepare(keep=False)


async def test_symbolic_ref_exiting_1_still_means_a_detached_checkout(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    failing_git(monkeypatch, "symbolic-ref", status=1)
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    merge = await tree.integrate()
    assert merge.result == "detached"
    assert merge.detail == (
        "There's no branch to merge into (detached HEAD); the work is on orbit/coder."
    )


async def test_a_git_failure_checking_for_the_coder_branch_fails_prepare_before_adding_a_tree(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    ran = failing_git(monkeypatch, "rev-parse", "--verify")
    tree = GitCoderTree(str(repo))
    with pytest.raises(TreeFailed, match="git rev-parse failed: fatal: bad object deadbeef"):
        await tree.prepare(keep=False)
    assert not [args for args in ran if args[:2] == ("worktree", "add")]


async def test_a_git_failure_checking_for_the_coder_branch_fails_leftovers(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    failing_git(monkeypatch, "rev-parse", "--verify")
    with pytest.raises(TreeFailed, match="git rev-parse failed: fatal: bad object deadbeef"):
        await tree.leftovers()


async def test_rev_parse_verify_exiting_1_still_means_a_missing_coder_branch(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    assert await tree.leftovers()
    failing_git(monkeypatch, "rev-parse", "--verify", status=1)
    assert await tree.leftovers() == []


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


def failing_git(
    monkeypatch: pytest.MonkeyPatch, *command: str, status: int = 128
) -> list[tuple[str, ...]]:
    """Make the git commands starting with ``command`` exit with ``status``, as on a corrupt
    repository; returns the git commands run."""
    ran: list[tuple[str, ...]] = []

    async def fake(cwd: Path | str, *args: str) -> GitResult:
        ran.append(args)
        if args[: len(command)] == command:
            return GitResult(status, b"", b"fatal: bad object deadbeef\n")
        return await run_git(cwd, *args)

    monkeypatch.setattr(git_tree, "git", fake)
    return ran


def failing_merge_base(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Make ``git merge-base`` fail as on a corrupt repository; returns the git commands run."""
    return failing_git(monkeypatch, "merge-base")


async def test_integrate_raises_when_git_cant_tell_an_ancestor_and_doesnt_rebase(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "b.txt").write_text("two\n")
    await tree.commit("Add b")
    ran = failing_merge_base(monkeypatch)
    with pytest.raises(TreeFailed, match="git merge-base failed: fatal: bad object deadbeef"):
        await tree.integrate()
    assert not [args for args in ran if args[0] in ("rebase", "merge", "update-ref")]


async def test_prepare_raises_when_git_cant_tell_an_ancestor_and_doesnt_catch_up(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    commit_file(repo, "c.txt", "three\n", "user's commit")
    ran = failing_merge_base(monkeypatch)
    with pytest.raises(TreeFailed, match="git merge-base failed: fatal: bad object deadbeef"):
        await tree.prepare(keep=False)
    assert not [args for args in ran if args[0] in ("rebase", "merge")]


async def test_holds_work_raises_when_git_cant_tell_an_ancestor(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    failing_merge_base(monkeypatch)
    with pytest.raises(TreeFailed, match="git merge-base failed: fatal: bad object deadbeef"):
        await tree.holds_work()


def warning_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every git command warn on stderr, as a misconfigured repository can."""

    async def fake(cwd: Path | str, *args: str) -> GitResult:
        result = await run_git(cwd, *args)
        return GitResult(result.status, result.output, b"warning: noise\n" + result.errors)

    monkeypatch.setattr(git_tree, "git", fake)


async def test_a_git_warning_doesnt_change_what_the_tree_reads(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    warning_git(monkeypatch)
    assert not await tree.holds_work()  # Clean, so not dirty despite the warning.
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    (repo / "a.txt").write_text("in the way\n")
    await tree.integrate()
    assert await tree.head() == git(path, "rev-parse", "HEAD")
    assert await tree.untracked() == frozenset()
    assert await tree.leftovers() == ["1 commit on orbit/coder not yet merged into main."]


async def test_a_failing_command_still_raises_with_gits_message_beside_a_warning(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    tree = GitCoderTree(str(repo))
    warning_git(monkeypatch)
    with pytest.raises(TreeFailed, match=r"git show failed: warning: noise\nfatal:"):
        await tree.show(["no-such-commit"])


async def test_a_tag_named_like_the_coder_branch_doesnt_change_the_leftovers(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    path = Path(await tree.prepare(keep=False))
    (path / "a.txt").write_text("orbit\n")
    await tree.commit("Orbit edits a")
    git(repo, "tag", "orbit/coder", "main")  # Behind the branch, so it would count none.
    assert await tree.leftovers() == ["1 commit on orbit/coder not yet merged into main."]


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
    with pytest.raises(TreeFailed, match="no network"):
        await tree.prepare(keep=False)
    with pytest.raises(TreeFailed, match="no network"):
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
        whole_files.write_whole,
    )

    def fingerprint(setup: Setup, path: Path) -> str:
        threads.append(threading.current_thread())
        return real_fingerprint(setup, path)

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "coder.synced":
            threads.append(threading.current_thread())
        return real_read(self, *args, **kwargs)

    def write_whole(path: Path, text: str) -> None:
        if path.name == "coder.synced":
            threads.append(threading.current_thread())
        real_write(path, text)

    monkeypatch.setattr(git_tree, "_fingerprint", fingerprint)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(git_tree, "write_whole", write_whole)
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
    with pytest.raises(TreeFailed, match="git"):
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


async def test_head_before_the_coder_tree_exists_raises_a_tree_failure(repo: Path) -> None:
    # git can't run in a directory that isn't there; callers expect a TreeFailed, not an OSError.
    tree = GitCoderTree(str(repo))
    with pytest.raises(TreeFailed, match=r"there.s no directory .*coder to run it in"):
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


async def test_a_locked_snapshot_is_dropped(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    snapshot = Path(await tree.snapshot("HEAD"))
    git(repo, "worktree", "lock", str(snapshot))

    await tree.drop(str(snapshot))
    assert not snapshot.exists()
    assert str(snapshot) not in git(repo, "worktree", "list")
    assert await tree.leftovers() == []


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
    with pytest.raises(TreeFailed, match="git rev-parse"):
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
        raise TreeFailed("git worktree failed: index.lock exists")

    monkeypatch.setattr(GitCoderTree, "_git", fake)


@pytest.mark.parametrize("after", [False, True])
async def test_a_failed_snapshot_leaves_no_directory_or_worktree(
    monkeypatch: pytest.MonkeyPatch, repo: Path, after: bool
) -> None:
    failing_add(monkeypatch, after=after)
    tree = GitCoderTree(str(repo))
    with pytest.raises(TreeFailed, match=r"index\.lock"):
        await tree.snapshot("HEAD")
    assert snapshot_dirs(repo) == []
    assert "snapshot-" not in git(repo, "worktree", "list")
    assert await GitCoderTree(str(repo)).leftovers() == []


async def test_a_failed_snapshot_left_locked_is_unregistered(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    failing_add(monkeypatch, after=True, lock=True)
    tree = GitCoderTree(str(repo))
    with pytest.raises(TreeFailed, match=r"index\.lock"):
        await tree.snapshot("HEAD")
    assert snapshot_dirs(repo) == []
    assert "snapshot-" not in git(repo, "worktree", "list", "--porcelain")
    assert list((repo / ".git" / "worktrees").glob("snapshot-*")) == []
    assert await GitCoderTree(str(repo)).leftovers() == []


async def test_a_failing_cleanup_doesnt_hide_why_the_snapshot_failed(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    failing_add(monkeypatch, after=False)
    real = run_git

    async def no_remove(cwd: Path | str, *args: str) -> GitResult:
        if args[:2] == ("worktree", "remove"):
            raise GitUnavailable("git worktree failed to start")
        return await real(cwd, *args)

    monkeypatch.setattr(git_tree, "git", no_remove)
    tree = GitCoderTree(str(repo))
    with pytest.raises(TreeFailed, match=r"index\.lock"):
        await tree.snapshot("HEAD")
    assert snapshot_dirs(repo) == [] and tree._snapshots == set()


async def test_an_unremovable_snapshot_directory_doesnt_hide_why_the_snapshot_failed(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    real = GitCoderTree._git

    async def locking_add(self: GitCoderTree, cwd: Path, *args: str) -> str:
        if args[:2] == ("worktree", "add"):
            Path(args[-2]).parent.chmod(0o555)
            raise TreeFailed("git worktree failed: index.lock exists")
        return await real(self, cwd, *args)

    monkeypatch.setattr(GitCoderTree, "_git", locking_add)
    tree = GitCoderTree(str(repo))
    try:
        with pytest.raises(TreeFailed, match=r"index\.lock"):
            await tree.snapshot("HEAD")
    finally:
        (repo / ".git" / "orbit").chmod(0o755)
    assert len(snapshot_dirs(repo)) == 1 and tree._snapshots == set()


async def test_a_snapshot_whose_directory_cant_be_made_raises(repo: Path) -> None:
    orbit = repo / ".git" / "orbit"
    orbit.mkdir()
    orbit.chmod(0o555)
    tree = GitCoderTree(str(repo))
    try:
        with pytest.raises(TreeFailed, match="Couldn't make a snapshot directory"):
            await tree.snapshot("HEAD")
    finally:
        orbit.chmod(0o755)
    assert tree._snapshots == set()


async def test_snapshots_that_cant_be_listed_raise(repo: Path) -> None:
    orbit = repo / ".git" / "orbit"
    orbit.mkdir()
    orbit.chmod(0o000)
    try:
        with pytest.raises(TreeFailed, match="Couldn't list the snapshots"):
            await GitCoderTree(str(repo)).leftovers()
    finally:
        orbit.chmod(0o755)


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


async def test_a_snapshot_cancelled_twice_while_making_its_directory_leaves_nothing(
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
    for _ in range(2):  # Again while it waits for the thread: it still removes the directory.
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
        "2 snapshot worktrees left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    ]
    assert await earlier.leftovers() == [
        "1 snapshot worktree left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    ]
    await tree.drop(own)
    assert await earlier.leftovers() == []  # Dropped, so no longer there.


async def test_leftovers_report_snapshots_of_the_old_naming(repo: Path) -> None:
    git(repo, "worktree", "add", "-q", "--detach", str(repo / ".git" / "orbit" / "review-HEAD-1"))
    notes = await GitCoderTree(str(repo)).leftovers()
    assert notes == [
        "1 snapshot worktree left in .git/orbit by earlier runs; "
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


async def test_ignoring_paths_keeps_the_exclude_files_mode(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.chmod(0o664)
    await tree.ignore({"__pycache__/"})
    assert "__pycache__/" in exclude.read_text()
    assert exclude.stat().st_mode & 0o777 == 0o664


async def test_ignoring_paths_that_cant_replace_the_exclude_file_leaves_it_whole(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log\n")

    def full(source: Path, target: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(TreeFailed, match="No space left on device"):
        await tree.ignore({"__pycache__/"})
    assert exclude.read_text() == "*.log\n"
    assert [p.name for p in exclude.parent.iterdir()] == ["exclude"]


async def test_ignoring_paths_that_fail_midway_leaves_the_exclude_file_whole(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log\n")

    def full(descriptor: int) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(TreeFailed, match="No space left on device"):
        await tree.ignore({"__pycache__/"})
    assert exclude.read_text() == "*.log\n"
    assert [p.name for p in exclude.parent.iterdir()] == ["exclude"]


async def test_ignoring_paths_that_cant_be_written_raises_a_tree_failure(repo: Path) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    info = repo / ".git" / "info"
    (info / "exclude").unlink()
    info.rmdir()
    info.write_text("")  # Not a directory any more.
    with pytest.raises(TreeFailed, match="Couldn't ignore files in"):
        await tree.ignore({"__pycache__/"})


async def test_ignoring_paths_with_a_non_utf8_exclude_file_raises_a_tree_failure(
    repo: Path,
) -> None:
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_bytes(b"# \xff\n")
    with pytest.raises(TreeFailed, match=r"Couldn't ignore files in .*exclude"):
        await tree.ignore({"__pycache__/"})


async def test_a_setup_stamp_that_cant_be_read_raises_a_tree_failure(repo: Path) -> None:
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    (path.parent / "coder.synced").mkdir()  # A directory in the stamp's place.
    setup, runs = counting_setup(repo)
    with pytest.raises(TreeFailed, match=r"Couldn't read the setup record in .*coder\.synced"):
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


async def test_a_non_utf8_setup_stamp_raises_a_tree_failure(repo: Path) -> None:
    path = Path(await GitCoderTree(str(repo)).prepare(keep=False))
    (path.parent / "coder.synced").write_bytes(b"\xff")
    setup, _ = counting_setup(repo)
    with pytest.raises(TreeFailed, match=r"Couldn't read the setup record in .*coder\.synced"):
        await GitCoderTree(str(repo), setup=setup).prepare(keep=False)


async def test_a_watched_file_that_cant_be_read_raises_a_tree_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_file(repo, "uv.lock", "v1\n", "lock")
    setup, runs = counting_setup(repo, watch=("uv.lock",))

    def read_bytes(self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(TreeFailed, match=r"Couldn't read .*uv\.lock to check the setup: denied"):
        await GitCoderTree(str(repo), setup=setup).prepare(keep=False)
    assert not runs.exists()


async def test_a_setup_that_cant_be_recorded_raises_a_tree_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, _ = counting_setup(repo)
    real_write = whole_files.write_whole

    def write_whole(path: Path, text: str) -> None:
        if path.name == "coder.synced":
            raise PermissionError("denied")
        real_write(path, text)

    monkeypatch.setattr(git_tree, "write_whole", write_whole)
    with pytest.raises(TreeFailed, match=r"Couldn't record the setup in .*coder\.synced: denied"):
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


async def test_a_missing_root_isnt_in_a_repository(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(TreeFailed, match=f"{missing} isn't in a git repository"):
        await GitCoderTree(str(missing)).head()


async def test_the_repositorys_git_directory_is_found_once(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    found: list[str] = []
    real = find_git_common_dir

    async def counting(path: str) -> str | None:
        found.append(path)
        return await real(path)

    monkeypatch.setattr(git_tree, "find_git_common_dir", counting)
    tree = GitCoderTree(str(repo))
    await tree.prepare(keep=False)
    await tree.head()
    await tree.leftovers()
    assert found == [str(repo)]


async def test_a_setup_that_cant_start_raises_a_tree_failure(repo: Path) -> None:
    tree = GitCoderTree(str(repo), Setup(command=("orbit-no-such-command", "--sync")))
    with pytest.raises(TreeFailed, match=r"^Could not start 'orbit-no-such-command': no such pro"):
        await tree.prepare(keep=False)
