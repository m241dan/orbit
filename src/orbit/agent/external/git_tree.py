"""The coder's git worktree: where agents work, apart from the user's checkout.

It lives inside the repository's git directory (``.git/orbit/coder``), on the branch
``orbit/coder``, so the main checkout never sees it. Its commits are merged into the branch
that was checked out when orbit started, fast-forward only. Work is never deleted: changes
left behind are stashed, and commits that can't be merged stay on ``orbit/coder``.
"""

import asyncio
import datetime
import hashlib
import itertools
import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path

from orbit.agent.application.ports import Merge
from orbit.agent.domain.roles import Setup
from orbit.agent.external.processes import run_process

BRANCH = "orbit/coder"
STASH_MESSAGE = "orbit: unverified work"
IGNORE_HEADER = "# Created by orbit's verification checks:"


def in_git_work_tree(path: str) -> bool:
    """Whether ``path`` is inside a git work tree (and git can be run)."""
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--is-inside-work-tree"),
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.stdout.strip() == "true"


def git_top_level(path: str) -> str | None:
    """The top level of the git work tree ``path`` is in; none outside one (or without git)."""
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


async def _run(command: Sequence[str], cwd: Path) -> tuple[bool, str]:
    """Run ``command`` in ``cwd``; returns whether it succeeded and its combined output.

    Raises ``RuntimeError`` when it can't be started, such as in a directory that isn't there.
    """
    try:
        returncode, output = await run_process(command, cwd)
    except OSError as error:
        raise RuntimeError(f"{' '.join(command[:2])} failed to start in {cwd}: {error}") from error
    return returncode == 0, output.decode(errors="replace").strip()


def _fingerprint(setup: Setup, path: Path) -> str:
    """A digest of ``setup``'s command and its watched files in ``path``, missing ones included."""
    digest = hashlib.sha256(repr(setup.command).encode())
    for name in setup.watch:
        file = path / name
        content = file.read_bytes() if file.is_file() else None
        digest.update(repr((name, content)).encode())
    return digest.hexdigest()


def _read_stamp(stamp: Path) -> str | None:
    """The digest ``stamp`` records; ``None`` when there isn't one."""
    return stamp.read_text() if stamp.exists() else None


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
        self._snapshots = itertools.count(1)

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
        path = await self._path()
        await self._git(path, "add", "-A")
        changed, _ = await _run(("git", "diff", "--cached", "--quiet"), path)
        if changed:  # Exit status 0: nothing staged.
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
            return await self._rebase(await self._path(), base)
        return await self._fast_forward(base, coder)

    async def head(self) -> str:
        return await self._git(await self._path(), "rev-parse", "HEAD")

    async def base_commit(self) -> str:
        base = await self._base_branch()
        return await self._git(self._root, "rev-parse", f"refs/heads/{base}" if base else "HEAD")

    async def show(self, commits: Sequence[str]) -> str:
        return await self._git(self._root, "show", "--stat", "--patch", "--format=medium", *commits)

    async def snapshot(self, commit: str) -> str:
        path = (await self._path()).parent / f"review-{commit[:12]}-{next(self._snapshots)}"
        await self._git(self._root, "worktree", "add", "-q", "--detach", str(path), commit)
        return str(path)

    async def drop(self, path: str) -> None:
        await self._git(self._root, "worktree", "remove", "--force", path)

    async def untracked(self) -> frozenset[str]:
        listed = await self._git(
            await self._path(), "ls-files", "--others", "--exclude-standard", "--directory"
        )
        return frozenset(listed.splitlines())

    async def ignore(self, paths: Collection[str]) -> None:
        exclude = (await self._path()).parent.parent / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        new = sorted(set(paths) - set(lines))
        if IGNORE_HEADER not in lines:
            new.insert(0, IGNORE_HEADER)
        exclude.write_text("\n".join([*lines, *new]) + "\n")

    def use_setup(self, setup: Setup | None) -> None:
        self.setup = setup

    async def leftovers(self) -> list[str]:
        notes = [await self._unmerged(), await self._stashed()]
        return [note for note in notes if note]

    async def _unmerged(self) -> str | None:
        base = await self._base_branch()
        if base is None or not await self._exists(BRANCH):
            return None
        count = int(await self._git(self._root, "rev-list", "--count", f"{base}..{BRANCH}"))
        if not count:
            return None
        return f"{count} commit{'s' if count > 1 else ''} on {BRANCH} not yet merged into {base}."

    async def _stashed(self) -> str | None:
        stashes = await self._git(self._root, "stash", "list", "--format=%s")
        count = sum(STASH_MESSAGE in line for line in stashes.splitlines())
        return f"{count} stash(es) of unverified orbit work; see git stash list." if count else None

    # Steps of prepare

    async def _ensure(self) -> Path:
        path = await self._path()
        if (path / ".git").exists():
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
            await self._rebase(path, base)  # A conflict leaves the work as it was.

    async def _set_up(self, path: Path) -> None:
        if self.setup is None:
            return
        digest = await asyncio.to_thread(_fingerprint, self.setup, path)
        stamp = path.parent / "coder.synced"
        if await asyncio.to_thread(_read_stamp, stamp) == digest:
            return
        ok, output = await _run(self.setup.command, path)
        if not ok:
            raise RuntimeError(f"Setting up the coder's tree failed: {output}")
        await asyncio.to_thread(stamp.write_text, digest)

    # Merging

    async def _rebase(self, path: Path, base: str) -> Merge:
        ok, _ = await _run(("git", "rebase", "-q", base), path)
        if ok:
            return Merge("rebased")
        conflicts = await self._git(path, "diff", "--name-only", "--diff-filter=U")
        await self._git(path, "rebase", "--abort")
        files = ", ".join(conflicts.splitlines())
        return Merge("conflict", f"The work on {BRANCH} conflicts with {self._base}: {files}.")

    async def _fast_forward(self, base: str, coder: str) -> Merge:
        assert self._base is not None
        if await self._checked_out() == self._base:
            ok, output = await _run(("git", "merge", "-q", "--ff-only", BRANCH), self._root)
            return Merge("merged") if ok else Merge("blocked", output)
        # Not checked out any more: move the branch, only if nobody else has.
        await self._git(self._root, "update-ref", f"refs/heads/{self._base}", coder, base)
        return Merge("merged")

    # Git

    async def _git(self, cwd: Path, *args: str) -> str:
        ok, output = await _run(("git", *args), cwd)
        if not ok:
            raise RuntimeError(f"git {args[0]} failed: {output}")
        return output

    async def _path(self) -> Path:
        common = await self._git(
            self._root, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        return Path(common) / "orbit" / "coder"

    async def _base_branch(self) -> str | None:
        """The base branch: the one checked out when the tree started, or now, before it has."""
        return self._base if self._started else await self._checked_out()

    async def _checked_out(self) -> str | None:
        """The branch the main checkout is on; ``None`` on a detached HEAD."""
        ok, output = await _run(("git", "symbolic-ref", "--short", "-q", "HEAD"), self._root)
        return output if ok else None

    async def _tip(self, branch: str) -> str:
        return await self._git(self._root, "rev-parse", f"refs/heads/{branch}")

    async def _exists(self, branch: str) -> bool:
        ok, _ = await _run(
            ("git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"), self._root
        )
        return ok

    async def _is_ancestor(self, older: str, newer: str, cwd: Path | None = None) -> bool:
        ok, _ = await _run(("git", "merge-base", "--is-ancestor", older, newer), cwd or self._root)
        return ok

    async def _dirty(self, path: Path) -> bool:
        return bool(await self._git(path, "status", "--porcelain"))
