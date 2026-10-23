"""The git repository a workspace is in: its top level, its shared git directory, what
identifies it, whether git has an identity for commits in it, and orbit's directory in it."""

from pathlib import Path

from orbit.agent.external.git import GitResult, GitUnavailable, git, git_sync

# Orbit's directory in the repository's shared git directory: the coder's worktree, review
# snapshots, leftovers and fallback files live in it, and older orbits' session files until
# they are imported.
ORBIT_DIR = "orbit"
_COMMON_DIR = ("rev-parse", "--path-format=absolute", "--git-common-dir")


def _answer(result: GitResult) -> str | None:
    """What ``result``'s git printed, stripped; ``None`` when it failed."""
    return result.text if result.ok else None


def _git_query(path: str, *args: str) -> str | None:
    """What ``git args`` prints in ``path``, stripped; ``None`` when it fails or can't be run."""
    try:
        return _answer(git_sync(path, *args, stderr=False))
    except GitUnavailable:
        return None


def in_git_work_tree(path: str) -> bool:
    """Whether ``path`` is inside a git work tree (and git can be run)."""
    return _git_query(path, "rev-parse", "--is-inside-work-tree") == "true"


def git_top_level(path: str) -> str | None:
    """The top level of the git work tree ``path`` is in; none outside one (or without git)."""
    return _git_query(path, "rev-parse", "--show-toplevel")


def git_common_dir(path: str) -> str | None:
    """The git directory the repository ``path`` is in shares among its worktrees; none outside
    one (or without git)."""
    return _git_query(path, *_COMMON_DIR)


async def find_git_common_dir(path: str) -> str | None:
    """:func:`git_common_dir`, without blocking."""
    try:
        return _answer(await git(path, *_COMMON_DIR, stderr=False))
    except GitUnavailable:
        return None


def has_git_identity(path: str) -> bool:
    """Whether git resolves an author and a committer for commits in the repository ``path``
    is in, from the environment or its config, as committing there needs (and git can be run)."""
    idents = ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")
    return all(_git_query(path, "var", ident) is not None for ident in idents)


class RepositoryUnreadable(OSError):
    """A workspace is in a git repository, but git can't say where its shared git directory
    is."""


def orbit_dir(workspace: str) -> Path:
    """Orbit's directory for ``workspace``: ``orbit`` in the repository's shared git directory,
    or ``.orbit`` in ``workspace`` outside a repository (with or without git).

    Raises :class:`RepositoryUnreadable` if ``workspace`` is in a repository, as a ``.git`` in
    it or a parent says, but git fails to find its shared git directory or can't be run.
    """
    try:
        result = git_sync(workspace, *_COMMON_DIR)
    except GitUnavailable as exc:
        return _outside_repository(Path(workspace), str(exc))
    if result.ok:
        return Path(result.text) / ORBIT_DIR
    return _outside_repository(Path(workspace), result.message)


def _outside_repository(workspace: Path, problem: str) -> Path:
    """Orbit's directory in ``workspace``, which git couldn't place in a repository, saying
    ``problem``. Raises :class:`RepositoryUnreadable` if it's in one all the same."""
    if any((place / ".git").exists() for place in (workspace, *workspace.absolute().parents)):
        raise RepositoryUnreadable(f"Git can't find the repository {workspace} is in: {problem}")
    return workspace / ".orbit"


def workspace_fingerprint(path: str) -> str | None:
    """What identifies the repository ``path`` is in wherever it's moved: its root commits'
    hashes and its ``origin`` remote's URL (empty without one), joined by spaces; none outside a
    repository, before its first commit or without git."""
    roots = _git_query(path, "rev-list", "--max-parents=0", "HEAD")
    if not roots:
        return None
    origin = _git_query(path, "remote", "get-url", "origin") or ""
    return " ".join([*sorted(roots.split()), origin])
