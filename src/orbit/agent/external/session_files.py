"""Where an older orbit saved sessions on disk, for importing them into the store.

Inside a git repository they lived in ``.git/orbit/sessions``, beside the coder's worktree, with
the review count in ``reviews.json`` beside the folder; outside one, in ``.orbit`` in the
workspace.
"""

from pathlib import Path

from orbit.agent.external.repository import orbit_dir

SESSIONS = "sessions"
REVIEWS = "reviews.json"


def legacy_dir(workspace: str) -> Path:
    """The directory an older orbit kept saved sessions in for ``workspace``: orbit's directory,
    ``orbit`` in the repository's shared git directory or ``.orbit`` outside one."""
    return orbit_dir(workspace)
