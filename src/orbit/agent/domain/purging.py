"""Purging a workspace: what deleting a clone orbit made would lose, what keeps it from being
purged and what purging it deleted."""

from collections.abc import Sequence
from dataclasses import dataclass

from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.workspaces import RegisteredWorkspace
from orbit.protocol import INVALID


class PurgeRefused(Refused):
    """A workspace isn't purged, for the reason the message gives, refused as ``code``, having
    deleted nothing."""

    def __init__(self, message: str, code: str = INVALID) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class Purged:
    """What purging the ``workspace`` deleted: its ``sessions``, its ``fallback_files`` and,
    for a clone orbit made, the ``clone`` directory."""

    workspace: RegisteredWorkspace
    sessions: int
    fallback_files: int
    clone: str | None


def _counted(count: int, one: str, many: str) -> str:
    """``count`` of something, called ``one`` or ``many`` as the count says."""
    return f"{count} {one if count == 1 else many}"


def clone_losses(
    changed: int, stashes: int, unpushed: Sequence[tuple[str, int]], detached: int
) -> list[str]:
    """What deleting a clone would lose, a short line each: its ``changed`` files not committed,
    its ``stashes``, for each branch in ``unpushed``, by name, the commits on it its remote
    doesn't have and the ``detached`` commits on its HEAD that neither a branch nor its remote
    has; none when it would lose nothing."""
    losses: list[str] = []
    if changed:
        losses.append(f"uncommitted changes to {_counted(changed, 'file', 'files')}")
    if stashes:
        losses.append(_counted(stashes, "stash", "stashes"))
    losses += [
        f"{_counted(count, 'commit', 'commits')} on {branch} not on its remote"
        for branch, count in unpushed
        if count
    ]
    if detached:
        losses.append(
            f"{_counted(detached, 'commit', 'commits')} on a detached HEAD not on its remote"
        )
    return losses


def unchecked_clone(problem: str) -> list[str]:
    """What deleting a clone whose work can't be checked, for the ``problem`` given, would lose,
    as far as anyone can tell: perhaps anything."""
    return [f"whatever it holds, which can't be checked: {problem}"]


def purge_refusal(workspace: str, losses: Sequence[str]) -> str:
    """Why the workspace ``workspace`` isn't purged: deleting its clone would lose the
    ``losses``."""
    return f"Can't purge {workspace}: deleting its clone would lose {'; '.join(losses)}."


def check_purgeable(workspace: str, losses: Sequence[str]) -> None:
    """Refuse purging the workspace ``workspace`` as ``invalid`` if deleting its clone would
    lose anything, listing the ``losses``."""
    if losses:
        raise PurgeRefused(purge_refusal(workspace, losses))


def unregistered(key: str) -> str:
    """Why the workspace whose path or id is ``key`` isn't purged: none is registered so."""
    return f"No workspace is registered at {key}."


def owned_refusal(workspace: str, owner: WorkspaceOwner) -> str:
    """Why the workspace ``workspace`` isn't purged: another orbit, the ``owner``, owns its
    repository."""
    return f"Can't purge {workspace}: {owner.described} owns its repository."


def locked_refusal(workspace: str, owner: WorkspaceOwner) -> str:
    """Why the workspace ``workspace`` isn't forgotten: another orbit, the ``owner``, holds its
    workspace lock."""
    return f"Can't forget {workspace}: {owner.described} holds its lock."


def served_refusal(workspace: str, url: str) -> str:
    """Why the workspace ``workspace`` isn't forgotten: the ``orbit serve`` running at ``url``
    serves it."""
    return (
        f"Can't forget {workspace}: the orbit serve at {url} serves it; remove it there with its"
        " sessions, or stop that server first."
    )
