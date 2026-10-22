"""Read-only snapshots of a commit, made for a run and dropped after it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from orbit.agent.application.ports import CoderTree, Publish, TreeFailed
from orbit.protocol import GIT_FAILED, ErrorOccurred


@asynccontextmanager
async def snapshot_of(tree: CoderTree, commit: str, publish: Publish) -> AsyncIterator[str]:
    """A snapshot of ``tree`` at ``commit``, dropped on the way out, however the body ends.

    Making it raises :class:`TreeFailed` as the tree does. A failure to drop it is published
    with ``publish`` rather than raised, so the body's result, or its exception, stands.
    """
    root = await tree.snapshot(commit)
    try:
        yield root
    finally:
        await _drop(tree, root, publish)


async def _drop(tree: CoderTree, root: str, publish: Publish) -> None:
    """Drop the snapshot at ``root``, publishing why with ``publish`` if it can't be."""
    try:
        await tree.drop(root)
    except TreeFailed as exc:
        message = f"The snapshot at {root} couldn't be removed and is left over: {exc}"
        await publish(ErrorOccurred(code=GIT_FAILED, message=message))
