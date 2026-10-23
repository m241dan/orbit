"""Purging a workspace, as ``orbit db forget`` and a server removing a workspace with ``purge``
both do: deleting everything orbit keeps of it, and, for a clone orbit made, its directory,
unless another orbit holds it or that would lose work its remote doesn't have."""

from orbit.agent.application.ports import WorkspaceDeletion, WorkspacePurging
from orbit.agent.domain.purging import Purged, PurgeRefused, check_purgeable, unregistered
from orbit.agent.domain.workspaces import RegisteredWorkspace
from orbit.protocol import BUSY


async def purge(purging: WorkspacePurging, key: str) -> Purged:
    """What purging the workspace whose path or id is ``key`` deleted: found, held, checked for
    what it would lose, stopped and deleted through ``purging``. Raises
    :class:`PurgeRefused`, having deleted nothing, as ``invalid`` if none is registered so or
    deleting its clone would lose work, and as ``busy`` if another orbit holds it; what
    ``purging`` raises if it fails."""
    workspace = await purging.find(key)
    if workspace is None:
        raise PurgeRefused(unregistered(key))
    async with purging.held(workspace) as holder:
        if holder is not None:
            raise PurgeRefused(holder, BUSY)
        # Checked last before the deletion, so no work done since is lost.
        check_purgeable(workspace.path, await purging.losses(workspace))
        await purging.stop(workspace)
        return await purging.delete(workspace)


async def erase(deletion: WorkspaceDeletion, workspace: RegisteredWorkspace) -> Purged:
    """What deleting what orbit keeps of ``workspace`` through ``deletion`` deleted: its files
    first, its fallback files, session locks and, if it's a clone orbit made, its directory,
    never a directory orbit didn't clone into, then its records, so a failure partway leaves it
    registered for purging again."""
    files = await deletion.remove_fallback_files(workspace)
    await deletion.remove_session_locks(workspace.id)
    clone = workspace.path if workspace.managed else None
    if clone is not None:
        await deletion.remove_clone(clone)
    sessions = await deletion.remove_records(workspace.id)
    return Purged(workspace, sessions, files, clone)
