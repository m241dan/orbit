"""Which served workspace a command goes to: the one its connection is attached to, which its
envelope may name."""

from collections.abc import Sequence

from orbit.agent.domain.refusals import Refused, unknown
from orbit.protocol import INVALID, UNKNOWN, WRONG_SESSION

ATTACH_FIRST = "Attach a session first."
"""Why a command for a workspace is refused from a connection attached to none."""


def served_workspace(workspace: str, served: Sequence[str]) -> str:
    """``workspace``, if it's one of the ids of the workspaces ``served``; refused as
    ``unknown`` otherwise, naming them."""
    if workspace in served:
        return workspace
    if not served:
        raise Refused(UNKNOWN, f"No workspace {workspace!r}; none is served.")
    raise unknown(f"No workspace {workspace!r}", _listing(served), served)


def addressed_workspace(addressed: str | None, attached: str | None, served: Sequence[str]) -> str:
    """The id of the workspace a command for the workspace ``addressed``, if it names one, goes
    to from a connection ``attached`` to one, if it is, of those ``served``: the one attached.
    Refused as ``unknown`` for a workspace not served, as ``invalid`` before the connection is
    attached, and as ``wrong_session`` for one served but not attached, naming both."""
    if addressed is not None:
        served_workspace(addressed, served)
    if attached is None:
        raise Refused(INVALID, ATTACH_FIRST)
    if addressed is not None and addressed != attached:
        message = (
            f"Workspace {addressed!r} isn't the one attached; this connection is attached to "
            f"{attached}."
        )
        raise Refused(WRONG_SESSION, message)
    return attached


def _listing(served: Sequence[str]) -> str:
    """What names the workspaces ``served``."""
    return "the workspace served is" if len(served) == 1 else "the workspaces served are"
