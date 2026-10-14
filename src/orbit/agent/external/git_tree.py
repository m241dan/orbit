"""The coder's git worktree: where agents work, apart from the user's checkout.

It lives inside the repository's git directory (``.git/orbit/coder``), on the branch
``orbit/coder``, so the main checkout never sees it. Its commits are merged into the branch
that was checked out when orbit started, fast-forward only. Work is never deleted: changes
left behind are stashed, and commits that can't be merged stay on ``orbit/coder``.
"""

import asyncio
import datetime
import hashlib
from collections.abc import Sequence
from pathlib import Path

from orbit.agent.application.ports import Merge

BRANCH = "orbit/coder"
STASH_MESSAGE = "orbit: unverified work"
# Run in the tree when its lock file changes, so its environment matches.
DEFAULT_SETUP = ("uv", "sync", "--frozen")
LOCK_FILE = "uv.lock"


async def _run(command: Sequence[str], cwd: Path) -> tuple[bool, str]:
    """Run ``command`` in ``cwd``; returns whether it succeeded and its combined output."""
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode == 0, output.decode(errors="replace").strip()


class GitCoderTree:
    """The coder's worktree of the repository at ``root``.

    ``setup`` runs in the tree whenever its ``uv.lock`` changes; ``None`` skips it.
    """

    def __init__(self, root: str, setup: Sequence[str] | None = DEFAULT_SETUP) -> None:
        self._root = Path(root)
        self._setup = tuple(setup) if setup else None
        self._started = False
        self._base: str | None = None

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

    async def leftovers(self) -> list[str]:
        notes = [await self._unmerged(), await self._stashed()]
        return [note for note in notes if note]

    async def _unmerged(self) -> str | None:
        base = self._base if self._started else await self._checked_out()
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
        lock = path / LOCK_FILE
        if self._setup is None or not lock.exists():
            return
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        stamp = path.parent / "coder.synced"
        if stamp.exists() and stamp.read_text() == digest:
            return
        ok, output = await _run(self._setup, path)
        if not ok:
            raise RuntimeError(f"Setting up the coder's tree failed: {output}")
        stamp.write_text(digest)

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
