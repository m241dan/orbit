"""The workspaces ``orbit serve`` serves: those added with ``--add``, by their paths or cloned
from their URLs, and those flagged served by an earlier run, each opened with its lock held
while it's served."""

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

import typer

from orbit.agent import (
    CloneFailed,
    Cloner,
    ControlPlane,
    ServedWorkspace,
    ServedWorkspaces,
    SqliteStore,
    StorageSettings,
    StoreError,
    WorkspaceHost,
    WorkspaceRegistry,
    WorkspaceUnavailable,
)
from orbit.cli.backend import _Backend, _maker, _open, _storage, _usage_source
from orbit.cli.errors import _exit_on


def _workspaces(
    stack: ExitStack,
    flags: _Backend,
    store: SqliteStore,
    storage: StorageSettings,
    clone_dir: Path,
) -> ServedWorkspaces:
    """The workspaces ``orbit serve`` serves from the ``store``, each with its host made as
    ``flags`` ask, keeping its sessions as ``storage`` says, and its lock held until it's
    removed or ``stack`` closes, those added by URL cloned into ``clone_dir``."""
    make = _maker(flags, storage, pick_session=False)
    source = ServedWorkspaces(store, make, Cloner(clone_dir), worktree=flags.worktree)
    stack.callback(source.let_go)
    return source


type Added = Path | str
"""A workspace added with ``--add``: the directory it's at, or the URL it's cloned from."""


@dataclass(frozen=True)
class _Serving:
    """What ``orbit serve`` serves: what ``build``s its control plane, the ``workspaces`` it
    starts serving, and what's ``served`` as frontends add and remove workspaces."""

    build: Callable[[], ControlPlane]
    workspaces: tuple[ServedWorkspace, ...]
    served: ServedWorkspaces


@contextmanager
def _serving(
    flags: _Backend, added: Sequence[Added], state: Path, user_config: Path, clone_dir: Path
) -> Iterator[_Serving]:
    """Run the block serving the ``added`` workspaces, those added by URL cloned first into
    ``clone_dir`` and marked managed, flagged as served in the store in ``state`` once every one
    is found to be servable, and those flagged before, with the backend ``flags`` ask for and
    the user config in ``user_config``, holding each one's workspace lock if no other orbit
    does; yields what's served, then lets the locks go and closes the store. A workspace
    flagged before whose directory is gone, or that can't be served, is skipped, with a
    warning.

    Exits with status 2 if the store can't be opened, a clone fails, or an ``added`` workspace
    can't be registered, locked, opened or flagged, git has no identity for its repository with
    the coder committing there, or its configuration is invalid, leaving each flagged as it was.
    """
    storage = _storage(user_config)
    usage_source = _usage_source(flags)
    with ExitStack() as stack:
        store = _open(state)
        stack.callback(lambda: asyncio.run(store.close()))
        source = _workspaces(stack, flags, store, storage, clone_dir)
        served = [_added(source, root) for root in dict.fromkeys(added)]
        with _exit_on(WorkspaceUnavailable):
            source.flag_served()
        served += _earlier(source, store)
        workspaces = tuple(workspace for workspace, _ in served)
        makers = [make for _, make in served]

        def build() -> ControlPlane:
            return ControlPlane([make() for make in makers], usage_source, source, store)

        yield _Serving(build, workspaces, source)


def _added(
    source: ServedWorkspaces, added: Added
) -> tuple[ServedWorkspace, Callable[[], WorkspaceHost]]:
    """The workspace ``added`` with ``--add``, at its directory or cloned from its URL by
    ``source`` and marked managed, served by ``source``, not yet flagged served.

    Exits with status 2 if the clone fails or it can't be served.
    """
    if isinstance(added, Path):
        root, managed = added, False
    else:
        root, managed = _cloned(source, added), True
    with _exit_on(WorkspaceUnavailable):
        return source.served(root, managed=managed)


def _cloned(source: ServedWorkspaces, url: str) -> Path:
    """The directory ``source`` cloned the repository at ``url`` into, saying so.

    Exits with status 2, with git's message, if the clone fails.
    """
    typer.echo(f"Cloning {url}…")
    with _exit_on(CloneFailed):
        root = Path(source.cloned(url))
    typer.echo(f"Cloned {url} into {root}.")
    return root


def _earlier(
    source: ServedWorkspaces, store: SqliteStore
) -> list[tuple[ServedWorkspace, Callable[[], WorkspaceHost]]]:
    """The workspaces flagged as served in ``store`` by an earlier run that ``source`` doesn't
    serve yet, served by it, but for those that are gone or can't be served, warning of each.

    Exits with status 2 if the store fails.
    """
    served: list[tuple[ServedWorkspace, Callable[[], WorkspaceHost]]] = []
    for workspace_id, root in _flagged(store):
        if source.serves(workspace_id):
            continue
        try:
            served.append(source.served(root))
        except WorkspaceUnavailable as exc:
            typer.echo(f"Warning: The workspace {root} isn't served: {exc}", err=True)
    return served


def _flagged(store: SqliteStore) -> list[tuple[str, Path]]:
    """The id and directory of each workspace flagged as served in ``store``, but for those
    that are gone, warning of each.

    Exits with status 2 if the store fails.
    """
    with _exit_on(StoreError):
        served = asyncio.run(WorkspaceRegistry(store).served())
    return [(workspace_id, Path(path)) for workspace_id, path in served if _present(Path(path))]


def _present(root: Path) -> bool:
    """Whether the workspace at ``root`` is still there, warning that it isn't served if not."""
    if root.is_dir():
        return True
    typer.echo(f"Warning: The workspace {root} is gone, so it isn't served.", err=True)
    return False
