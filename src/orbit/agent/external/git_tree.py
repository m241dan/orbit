"""The coder's git worktree: where agents work, apart from the user's checkout.

It lives inside the repository's git directory (``.git/orbit/coder``), on the branch
``orbit/coder``, so the main checkout never sees it. Its commits are merged into the branch
that was checked out when orbit started, fast-forward only. Work is never deleted: changes
left behind are stashed, and commits that can't be merged stay on ``orbit/coder``.
"""

import asyncio
import contextlib
import datetime
import hashlib
import shutil
import tempfile
from collections.abc import Collection, Sequence
from pathlib import Path

from orbit.agent.application.ports import Merge, TreeFailed
from orbit.agent.domain.checks import Setup
from orbit.agent.external.git import git
from orbit.agent.external.processes import run_process
from orbit.agent.external.repository import ORBIT_DIR, find_git_common_dir
from orbit.agent.external.whole_files import write_whole

BRANCH = "orbit/coder"
STASH_MESSAGE = "orbit: unverified work"
IGNORE_HEADER = "# Created by orbit's verification checks:"
# Snapshot directories in ``.git/orbit``: ``snapshot-…`` now, ``review-…`` from older versions.
SNAPSHOT_PREFIXES = ("snapshot-", "review-")


async def _run_setup(command: Sequence[str], cwd: Path) -> tuple[bool, str]:
    """Run the setup ``command`` in ``cwd``; returns whether it succeeded and its combined
    output.

    Raises ``TreeFailed`` when it can't be started, such as in a directory that isn't there.
    """
    try:
        returncode, output = await run_process(command, cwd)
    except OSError as error:
        raise TreeFailed(f"{' '.join(command[:2])} failed to start in {cwd}: {error}") from error
    return returncode == 0, output.decode(errors="replace").strip()


def _fingerprint(setup: Setup, path: Path) -> str:
    """A digest of ``setup``'s command and its watched files in ``path``, missing ones included."""
    digest = hashlib.sha256(repr(setup.command).encode())
    for name in setup.watch:
        file = path / name
        try:
            content = file.read_bytes() if file.is_file() else None
        except OSError as error:
            raise TreeFailed(f"Couldn't read {file} to check the setup: {error}") from error
        digest.update(repr((name, content)).encode())
    return digest.hexdigest()


def _read_stamp(stamp: Path) -> str | None:
    """The digest ``stamp`` records; ``None`` when there isn't one, so the setup runs.

    Raises ``TreeFailed`` when it's there but can't be read.
    """
    try:
        return stamp.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise TreeFailed(f"Couldn't read the setup record in {stamp}: {error}") from error


def _add_ignores(exclude: Path, paths: Collection[str]) -> None:
    """Add ``paths`` to the git exclude file ``exclude`` under orbit's header, once each.

    Raises ``TreeFailed`` when the file can't be read or written.
    """
    try:
        exclude.parent.mkdir(exist_ok=True)
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        new = sorted(set(paths) - set(lines))
        if IGNORE_HEADER not in lines:
            new.insert(0, IGNORE_HEADER)
        write_whole(exclude, "\n".join([*lines, *new]) + "\n")
    except (OSError, UnicodeDecodeError) as error:
        raise TreeFailed(f"Couldn't ignore files in {exclude}: {error}") from error


def _make_snapshot_dir(parent: Path, commit: str) -> Path:
    """A new, empty directory in ``parent`` for a snapshot of ``commit``, named so it can't
    collide with any other, from this process or another.

    Raises ``TreeFailed`` when it can't be made.
    """
    try:
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"snapshot-{commit[:12]}-", dir=parent))
    except OSError as error:
        raise TreeFailed(f"Couldn't make a snapshot directory in {parent}: {error}") from error


def _snapshot_dirs(parent: Path) -> set[Path]:
    """The snapshot directories in ``parent``, by whoever made them; none when it's missing.

    Raises ``TreeFailed`` when ``parent`` can't be listed.
    """
    try:
        if not parent.is_dir():
            return set()
        return {
            path
            for path in parent.iterdir()
            if path.is_dir() and path.name.startswith(SNAPSHOT_PREFIXES)
        }
    except OSError as error:
        raise TreeFailed(f"Couldn't list the snapshots in {parent}: {error}") from error


class GitCoderTree:
    """The coder's worktree of the repository at ``root``.

    ``setup`` runs in the tree once, then whenever a file it watches changes; ``None`` skips it.
    """

    def __init__(self, root: str, setup: Setup | None = None) -> None:
        self._root = Path(root)
        self.setup = setup
        self.branch = BRANCH
        self._started = False
        self._base: str | None = None
        self._snapshots: set[Path] = set()
        self._common: Path | None = None

    async def prepare(self, keep: bool) -> str:
        if not self._started:
            self._base = await self._checked_out()
            self._started = True
        path = await self._ensure()
        if not keep:
            await self._put_aside(path)
        await self._catch_up(path)
        await self._set_up(path)
        return str(path)

    async def commit(self, message: str) -> bool:
        path = await self._coder()
        await self._git(path, "add", "-A")
        if await self._ask(path, "diff", "--cached", "--quiet"):  # Yes: nothing is staged.
            return False
        await self._git(path, "commit", "-q", "-m", message)
        return True

    async def integrate(self) -> Merge:
        if self._base is None:
            detail = f"There's no branch to merge into (detached HEAD); the work is on {BRANCH}."
            return Merge("detached", detail)
        base, coder = await self._tip(self._base), await self._tip(BRANCH)
        if await self._is_ancestor(coder, base):
            return Merge("merged")
        if not await self._is_ancestor(base, coder):
            return await self._rebase(await self._coder(), base)
        return await self._fast_forward(base, coder)

    async def holds_work(self) -> bool:
        path = await self._coder()
        if await self._dirty(path):
            return True
        return not await self._is_ancestor("HEAD", await self.base_commit(), path)

    async def head(self) -> str:
        return await self._git(await self._coder(), "rev-parse", "HEAD")

    async def base_commit(self) -> str:
        base = await self._base_branch()
        return await self._git(self._root, "rev-parse", f"refs/heads/{base}" if base else "HEAD")

    async def show(self, commits: Sequence[str]) -> str:
        return await self._git(self._root, "show", "--stat", "--patch", "--format=medium", *commits)

    async def snapshot(self, commit: str) -> str:
        sha = await self._git(self._root, "rev-parse", "--verify", "-q", f"{commit}^{{commit}}")
        path = await self._new_snapshot_dir(await self._snapshots_dir(), sha)
        self._snapshots.add(path)
        try:
            await self._git(self._root, "worktree", "add", "-q", "--detach", str(path), sha)
        except BaseException:  # Cancellation too: the caller never gets the path to drop.
            await self._discard_snapshot(path)
            raise
        return str(path)

    async def drop(self, path: str) -> None:
        # Twice forced: a snapshot may be locked, as a killed ``git worktree add`` leaves it.
        await self._git(self._root, "worktree", "remove", "--force", "--force", path)
        self._snapshots.discard(Path(path))

    async def _new_snapshot_dir(self, parent: Path, sha: str) -> Path:
        """A new directory in ``parent`` for a snapshot of ``sha``; when cancelled, the one
        the thread goes on to make is removed before the cancellation is raised."""
        making = asyncio.ensure_future(asyncio.to_thread(_make_snapshot_dir, parent, sha))
        try:
            return await asyncio.shield(making)
        except asyncio.CancelledError:
            await asyncio.wait([making])
            with contextlib.suppress(TreeFailed):  # It failed to make one: nothing to remove.
                await asyncio.to_thread(shutil.rmtree, making.result(), ignore_errors=True)
            raise

    async def _discard_snapshot(self, path: Path) -> None:
        """Remove the half-made snapshot at ``path``, whether or not git registered it, even
        locked as a killed ``git worktree add`` leaves it, ignoring failures so the error that
        stopped it is the one raised."""
        try:
            with contextlib.suppress(TreeFailed):
                await git(self._root, "worktree", "remove", "--force", "--force", str(path))
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        finally:
            self._snapshots.discard(path)

    async def untracked(self) -> frozenset[str]:
        listed = await self._git(
            await self._coder(), "ls-files", "--others", "--exclude-standard", "--directory"
        )
        return frozenset(listed.splitlines())

    async def ignore(self, paths: Collection[str]) -> None:
        await asyncio.to_thread(_add_ignores, await self._exclude(), paths)

    def use_setup(self, setup: Setup | None) -> None:
        self.setup = setup

    async def leftovers(self) -> list[str]:
        notes = [await self._unmerged(), await self._stashed(), await self._stale_snapshots()]
        return [note for note in notes if note]

    async def _unmerged(self) -> str | None:
        base = await self._base_branch()
        if base is None or not await self._exists(BRANCH):
            return None
        count = int(
            await self._git(
                self._root, "rev-list", "--count", f"refs/heads/{base}..refs/heads/{BRANCH}"
            )
        )
        if not count:
            return None
        return f"{count} commit{'s' if count > 1 else ''} on {BRANCH} not yet merged into {base}."

    async def _stashed(self) -> str | None:
        stashes = await self._git(self._root, "stash", "list", "--format=%s")
        count = sum(STASH_MESSAGE in line for line in stashes.splitlines())
        return f"{count} stash(es) of unverified orbit work; see git stash list." if count else None

    async def _stale_snapshots(self) -> str | None:
        snapshots = await asyncio.to_thread(_snapshot_dirs, await self._snapshots_dir())
        count = len(snapshots - self._snapshots)
        if not count:
            return None
        return (
            f"{count} snapshot worktree(s) left in .git/orbit by earlier runs; "
            "remove them with git worktree remove."
        )

    # Steps of prepare

    async def _ensure(self) -> Path:
        path = await self._coder()
        if await asyncio.to_thread((path / ".git").exists):
            await self._abort_rebase(path)  # One an interrupted run left, so its work survives.
            return path
        await self._git(self._root, "worktree", "prune")
        if await self._exists(BRANCH):
            await self._git(self._root, "worktree", "add", "-q", str(path), BRANCH)
        else:
            start = self._base or "HEAD"
            await self._git(self._root, "worktree", "add", "-q", "-b", BRANCH, str(path), start)
        return path

    async def _put_aside(self, path: Path) -> None:
        if await self._dirty(path):
            when = datetime.datetime.now().isoformat(timespec="seconds")
            await self._git(path, "stash", "push", "-q", "-u", "-m", f"{STASH_MESSAGE} ({when})")

    async def _catch_up(self, path: Path) -> None:
        """Bring the tree up to the base branch, when it's clean and there is one."""
        if self._base is None or await self._dirty(path):
            return
        base = await self._tip(self._base)
        if await self._is_ancestor(base, "HEAD", path):
            return
        if await self._is_ancestor("HEAD", base, path):
            await self._git(path, "merge", "-q", "--ff-only", base)
        else:
            # A conflict leaves the work as it was, for integrate to report once it's verified.
            _ = await self._rebase(path, base)

    async def _set_up(self, path: Path) -> None:
        if self.setup is None:
            return
        digest = await asyncio.to_thread(_fingerprint, self.setup, path)
        stamp = await self._stamp()
        if await asyncio.to_thread(_read_stamp, stamp) == digest:
            return
        ok, output = await _run_setup(self.setup.command, path)
        if not ok:
            raise TreeFailed(f"Setting up the coder's tree failed: {output}")
        try:
            await asyncio.to_thread(write_whole, stamp, digest)
        except OSError as error:
            raise TreeFailed(f"Couldn't record the setup in {stamp}: {error}") from error

    # Merging

    async def _rebase(self, path: Path, base: str) -> Merge:
        """Replay the tree's work onto ``base``; a conflict leaves it as it was.

        Raises ``TreeFailed`` with git's own message when the rebase fails without one.
        """
        try:
            rebase = await git(path, "rebase", "-q", base)
            if rebase.ok:
                return Merge("rebased")
            conflicts = await self._git(path, "diff", "--name-only", "--diff-filter=U")
        finally:  # Cancelled or failed too: never leave the tree mid-rebase.
            await self._abort_rebase_shielded(path)
        if not conflicts:
            raise TreeFailed(f"git rebase failed: {rebase.message}")
        files = ", ".join(conflicts.splitlines())
        return Merge("conflict", f"The work on {BRANCH} conflicts with {self._base}: {files}.")

    async def _abort_rebase_shielded(self, path: Path) -> None:
        """Abort the rebase in progress in ``path``, if there is one, finishing even when
        cancelled, and ignoring failures so the error that stopped it is the one raised."""
        aborting = asyncio.ensure_future(self._try_abort_rebase(path))
        try:
            await asyncio.shield(aborting)
        except asyncio.CancelledError:
            await asyncio.wait([aborting])
            raise

    async def _try_abort_rebase(self, path: Path) -> None:
        """Abort the rebase in progress in ``path``, if there is one, ignoring failures."""
        with contextlib.suppress(TreeFailed):
            await self._abort_rebase(path)

    async def _abort_rebase(self, path: Path) -> None:
        """Abort the rebase in progress in ``path``, if there is one."""
        states = await self._git(
            path, "rev-parse", "--git-path", "rebase-merge", "--git-path", "rebase-apply"
        )
        for state in states.splitlines():
            if await asyncio.to_thread((path / state).exists):
                await self._git(path, "rebase", "--abort")
                return

    async def _fast_forward(self, base: str, coder: str) -> Merge:
        assert self._base is not None
        if await self._checked_out() == self._base:
            merge = await git(self._root, "merge", "-q", "--ff-only", BRANCH)
            return Merge("merged") if merge.ok else Merge("blocked", merge.message)
        # Not checked out any more: move the branch, only if nobody else has.
        await self._git(self._root, "update-ref", f"refs/heads/{self._base}", coder, base)
        return Merge("merged")

    # Git

    async def _git(self, cwd: Path, *args: str) -> str:
        """What ``git args`` prints to stdout in ``cwd``.

        Raises ``TreeFailed`` with git's message when it fails.
        """
        result = await git(cwd, *args)
        if not result.ok:
            raise TreeFailed(f"git {args[0]} failed: {result.message}")
        return result.text

    async def _ask(self, cwd: Path, *args: str) -> bool:
        """Git's answer to the yes-or-no question ``git args`` in ``cwd``: exit 0 for yes, 1 for
        no.

        Raises ``TreeFailed`` with git's message when it exits otherwise, unable to tell.
        """
        result = await git(cwd, *args)
        if result.status not in (0, 1):
            raise TreeFailed(f"git {args[0]} failed: {result.message}")
        return result.ok

    # Paths

    async def _common_dir(self) -> Path:
        """The repository's shared git directory, found once."""
        if self._common is None:
            common = await find_git_common_dir(str(self._root))
            if common is None:
                raise TreeFailed(f"{self._root} isn't in a git repository, or git can't be run.")
            self._common = Path(common)
        return self._common

    async def _snapshots_dir(self) -> Path:
        """Orbit's directory in the repository, where the coder's tree and snapshots live."""
        return await self._common_dir() / ORBIT_DIR

    async def _coder(self) -> Path:
        """The coder's tree."""
        return await self._snapshots_dir() / "coder"

    async def _stamp(self) -> Path:
        """The record of the setup last run in the coder's tree."""
        return await self._snapshots_dir() / "coder.synced"

    async def _exclude(self) -> Path:
        """The repository's git exclude file."""
        return await self._common_dir() / "info" / "exclude"

    # Refs

    async def _base_branch(self) -> str | None:
        """The base branch: the one checked out when the tree started, or now, before it has."""
        return self._base if self._started else await self._checked_out()

    async def _checked_out(self) -> str | None:
        """The branch the main checkout is on; ``None`` on a detached HEAD."""
        result = await git(self._root, "symbolic-ref", "--short", "-q", "HEAD")
        return result.text if result.ok else None

    async def _tip(self, branch: str) -> str:
        return await self._git(self._root, "rev-parse", f"refs/heads/{branch}")

    async def _exists(self, branch: str) -> bool:
        return (await git(self._root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")).ok

    async def _is_ancestor(self, older: str, newer: str, cwd: Path | None = None) -> bool:
        return await self._ask(cwd or self._root, "merge-base", "--is-ancestor", older, newer)

    async def _dirty(self, path: Path) -> bool:
        return bool(await self._git(path, "status", "--porcelain"))
