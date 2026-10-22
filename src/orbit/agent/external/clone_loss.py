"""What deleting a clone orbit made would lose, as git sees it: uncommitted changes, stashes, and
commits on its branches that its remote doesn't have."""

import asyncio
from pathlib import Path

from orbit.agent.application.ports import TreeFailed
from orbit.agent.domain.purging import clone_losses, unchecked_clone
from orbit.agent.external.git import git

_BRANCHES = ("for-each-ref", "--format=%(refname:short)", "refs/heads")


async def clone_loss(path: Path) -> list[str]:
    """What deleting the clone at ``path`` would lose, a short line each, as
    :func:`clone_losses` words it; none when it would lose nothing, as when it's gone, and a
    line saying so when git can't tell."""
    if not await asyncio.to_thread(path.is_dir):
        return []
    try:
        changed = await _lines(path, "status", "--porcelain")
        stashes = await _lines(path, "stash", "list")
        branches = await _lines(path, *_BRANCHES)
        unpushed = [(branch, await _unpushed(path, branch)) for branch in branches]
    except TreeFailed as exc:
        return unchecked_clone(str(exc))
    return clone_losses(len(changed), len(stashes), unpushed)


async def _unpushed(path: Path, branch: str) -> int:
    """How many commits on ``branch`` in the clone at ``path`` none of its remotes has."""
    (count,) = await _lines(
        path, "rev-list", "--count", f"refs/heads/{branch}", "--not", "--remotes"
    )
    return int(count)


async def _lines(path: Path, *args: str) -> list[str]:
    """The lines ``git args`` prints in ``path``, but for blank ones. Raises
    :class:`TreeFailed` with git's message if it fails."""
    result = await git(path, *args)
    if not result.ok:
        raise TreeFailed(result.message)
    return [line for line in result.output.decode(errors="replace").splitlines() if line.strip()]
