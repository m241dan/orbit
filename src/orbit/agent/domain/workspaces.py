"""Which served workspace a command goes to: the one its connection is attached to, which its
envelope may name; and which workspaces may be added and removed."""

from collections.abc import Sequence
from enum import Enum, auto

from orbit.agent.domain.activity import Activity
from orbit.agent.domain.refusals import Refused, joined, unknown
from orbit.protocol import BUSY, INVALID, UNKNOWN, WRONG_SESSION

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


LOCAL_ONLY = "A local orbit works on one repository; run orbit serve to work on several."
"""Why a local orbit refuses adding and removing workspaces."""


class PathKind(Enum):
    """What there is at a path: nothing, a file (or anything but a directory) or a
    directory."""

    MISSING = auto()
    FILE = auto()
    DIRECTORY = auto()


def check_addable(path: str, *, absolute: bool) -> None:
    """Refuse adding the workspace at ``path`` as ``invalid`` unless it's ``absolute``."""
    if not absolute:
        raise Refused(INVALID, f"Can't add {path}: give the absolute path to a directory.")


def check_directory(path: str, kind: PathKind) -> None:
    """Refuse adding the workspace at ``path``, where there's what ``kind`` says, unless it's a
    directory: as ``unknown`` when there's nothing there, as ``invalid`` for a file."""
    if kind is PathKind.MISSING:
        raise Refused(UNKNOWN, f"Can't add {path}: there's nothing there.")
    if kind is PathKind.FILE:
        raise Refused(INVALID, f"Can't add {path}: it isn't a directory.")


def check_removable(workspace: str, activity: Activity, viewed: Sequence[str]) -> None:
    """Refuse removing the workspace ``workspace`` as ``busy`` while its open session runs what
    ``activity`` says, or other connections view the sessions ``viewed``, naming them."""
    work = _work(activity)
    if work is not None:
        raise Refused(BUSY, f"Can't remove {workspace}: {work}.")
    if viewed:
        sessions = "session" if len(viewed) == 1 else "sessions"
        views = f"another frontend views its {sessions} {joined(viewed)}"
        raise Refused(BUSY, f"Can't remove {workspace}: {views}.")


def _work(activity: Activity) -> str | None:
    """What runs of a task run, a review and a planner turn in ``activity``, the first of them;
    none when nothing does."""
    works = (
        (activity.tasks, "tasks are running"),
        (activity.review, "a review is running"),
        (activity.planner, "the planner is answering"),
    )
    return next((work for runs, work in works if runs), None)
