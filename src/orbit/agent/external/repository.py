"""The git repository a workspace is in: its top level, its shared git directory, what
identifies it, whether git has an identity for commits in it, and orbit's directory in it."""

import subprocess
from pathlib import Path

# Orbit's directory in the repository's shared git directory: the coder's worktree, review
# snapshots, leftovers and fallback files live in it, and older orbits' session files until
# they are imported.
ORBIT_DIR = "orbit"


def _git_query(path: str, *args: str) -> str | None:
    """What ``git args`` prints in ``path``, stripped; ``None`` when it fails or git is missing."""
    try:
        result = subprocess.run(
            ("git", *args), cwd=path, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def in_git_work_tree(path: str) -> bool:
    """Whether ``path`` is inside a git work tree (and git can be run)."""
    return _git_query(path, "rev-parse", "--is-inside-work-tree") == "true"


def git_top_level(path: str) -> str | None:
    """The top level of the git work tree ``path`` is in; none outside one (or without git)."""
    return _git_query(path, "rev-parse", "--show-toplevel")


def git_common_dir(path: str) -> str | None:
    """The git directory the repository ``path`` is in shares among its worktrees; none outside
    one (or without git)."""
    return _git_query(path, "rev-parse", "--path-format=absolute", "--git-common-dir")


def has_git_identity(path: str) -> bool:
    """Whether git resolves an author and a committer for commits in the repository ``path``
    is in, from the environment or its config, as committing there needs (and git can be run)."""
    idents = ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")
    return all(_git_query(path, "var", ident) is not None for ident in idents)


def orbit_dir(workspace: str) -> Path:
    """Orbit's directory for ``workspace``: ``orbit`` in the repository's shared git directory,
    or ``.orbit`` in ``workspace`` outside a repository (or without git)."""
    common = git_common_dir(workspace)
    return Path(common) / ORBIT_DIR if common else Path(workspace) / ".orbit"


def workspace_fingerprint(path: str) -> str | None:
    """What identifies the repository ``path`` is in wherever it's moved: its root commits'
    hashes and its ``origin`` remote's URL (empty without one), joined by spaces; none outside a
    repository, before its first commit or without git."""
    roots = _git_query(path, "rev-list", "--max-parents=0", "HEAD")
    if not roots:
        return None
    origin = _git_query(path, "remote", "get-url", "origin") or ""
    return " ".join([*sorted(roots.split()), origin])
