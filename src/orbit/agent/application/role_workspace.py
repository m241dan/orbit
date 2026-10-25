"""Where an agent works, as its role's ``workspace`` says: the coder's tree, a fresh snapshot of
the base commit, the user's checkout, an empty temporary directory or a fresh copy of the
workspace, a run's own directory removed after it, however it ends."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.ports import Copies, Publish, TempDirs, TreeFailed
from orbit.agent.application.role_copies import fresh_copy
from orbit.agent.application.snapshots import snapshot_of
from orbit.agent.domain.roles import Workspace

NO_COPIES = "This workspace can't make copies for a role whose workspace is copy."


@asynccontextmanager
async def workspace_for(
    where: Workspace, desk: AgentDesk, keep: bool = False
) -> AsyncIterator[str]:
    """The directory an agent whose role works ``where`` runs in with ``desk``'s agents: the
    coder's tree, readied for a task, keeping the changes left in it if ``keep``; a fresh
    snapshot of the base commit, dropped on the way out; the workspace's root, the user's
    checkout; a new empty temporary directory, removed on the way out; or a fresh copy of the
    workspace, removed on the way out. Without a tree, the worktree and a snapshot are the root,
    as agents work in place there.

    Readying the tree, making a snapshot or making a copy raises :class:`TreeFailed` as the
    tree or the copies do, as does a copy without agents that make them, and making a temporary
    directory :class:`TempDirFailed`.
    """
    agents = desk.agents
    tree = agents.tree
    if where == "copy":
        async with _copy(agents.copies, desk.publish) as path:
            yield path
    elif where == "none":
        async with _temp_dir(agents.temp_dirs) as path:
            yield path
    elif where == "checkout" or tree is None:
        yield agents.root
    elif where == "worktree":
        yield await tree.prepare(keep)
    else:
        async with snapshot_of(tree, await tree.base_commit(), desk.publish) as path:
            yield path


@asynccontextmanager
async def _temp_dir(temp_dirs: TempDirs) -> AsyncIterator[str]:
    """A new empty directory from ``temp_dirs``, removed on the way out, however the body
    ends."""
    path = await temp_dirs.make()
    try:
        yield path
    finally:
        await temp_dirs.remove(path)


@asynccontextmanager
async def _copy(copies: Copies | None, publish: Publish) -> AsyncIterator[str]:
    """A fresh copy from ``copies``, removed on the way out, as :func:`fresh_copy` makes it;
    raises :class:`TreeFailed` without ``copies``."""
    if copies is None:
        raise TreeFailed(NO_COPIES)
    async with fresh_copy(copies, publish) as path:
        yield path
