"""Where an older orbit saved sessions on disk, for importing them into the store.

Inside a git repository they lived in ``.git/orbit/sessions``, beside the coder's worktree, with
the review count in ``reviews.json`` beside the folder; outside one, in ``.orbit`` in the
workspace.
"""

from pathlib import Path

from orbit.agent.external.git_tree import ORBIT_DIR, git_common_dir

SESSIONS = "sessions"
REVIEWS = "reviews.json"


def legacy_dir(workspace: str) -> Path:
    """The directory an older orbit kept saved sessions in for ``workspace``: ``orbit`` in the
    repository's shared git directory, or ``.orbit`` in ``workspace`` outside a repository (or
    without git)."""
    common = git_common_dir(workspace)
    return Path(common) / ORBIT_DIR if common else Path(workspace) / ".orbit"
