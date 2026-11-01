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
    Browsing,
    CloneFailed,
    Cloner,
    Configuring,
    ControlPlane,
    PlaneParts,
    ServedWorkspace,
    ServedWorkspaces,
    SqliteStore,
    WorkspaceHost,
    WorkspaceUnavailable,
)
from orbit.cli.backend import _Backend, _maker, _open, _usage_source, _user_config, _UserConfig
from orbit.cli.errors import _exit_on


def _workspaces(
    stack: ExitStack,
    flags: _Backend,
    store: SqliteStore,
    user: _UserConfig,
    clone_dir: Path,
) -> ServedWorkspaces:
    """The workspaces ``orbit serve`` serves from the ``store``, each configured over the
    ``user`` config, with its host made as ``flags`` ask, keeping its sessions as the ``user``
    config says, and its lock held until it's removed or ``stack`` closes, those added by URL
    cloned into ``clone_dir`` and, unless they're kept, removed when ``stack`` closes."""
    make = _maker(flags, user, pick_session=False)
    configuring = Configuring(user.directory, flags.worktree, flags.runtime)
    source = ServedWorkspaces(store, make, Cloner(clone_dir), configuring=configuring)
    stack.callback(source.drop_clones)
    stack.callback(source.let_go)
    return source


type Added = Path | str
"""A workspace added with ``--add``: the directory it's at, or the URL it's cloned from."""


@dataclass(frozen=True)
class _Serving:
    """What ``orbit serve`` serves: what ``build``s its control plane, letting frontends browse
    as it's given, the ``workspaces`` it starts serving, and what's ``served`` as frontends add
    and remove workspaces."""

    build: Callable[[Browsing], ControlPlane]
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

    Exits with status 2 if the store can't be opened or the workspaces flagged before can't be
    read, a clone fails, or an ``added`` workspace can't be registered, locked, opened or
    flagged, git has no identity for its repository with the coder committing there, or its
    configuration is invalid, leaving each flagged as it was and removing the clones it made.
    """
    user = _user_config(user_config)
    usage_source = _usage_source(flags)
    with ExitStack() as stack:
        store = _open(state)
        stack.callback(lambda: asyncio.run(store.close()))
        source = _workspaces(stack, flags, store, user, clone_dir)
        # Those added are served first, so restoring flags them and doesn't serve them twice.
        served = [_added(source, root) for root in dict.fromkeys(added)] + _restore_earlier(source)
        workspaces = tuple(workspace for workspace, _ in served)
        makers = [make for _, make in served]

        def build(browsing: Browsing) -> ControlPlane:
            hosts = [make() for make in makers]
            parts = PlaneParts(
                mark=store, ledger=store, telemetry=flags.telemetry, gauges=flags.gauges
            )
            return ControlPlane(hosts, usage_source, source, browsing, parts)

        source.keep_clones()
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


def _restore_earlier(
    source: ServedWorkspaces,
) -> list[tuple[ServedWorkspace, Callable[[], WorkspaceHost]]]:
    """The workspaces flagged as served by an earlier run that ``source`` doesn't serve yet,
    served by it once it has flagged those it serves, but for those that are gone or can't be
    served, warning of each.

    Exits with status 2 if the store fails as the flags are read or set.
    """
    with _exit_on(WorkspaceUnavailable):
        restored = source.flag_and_restore()
    for warning in restored.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    return list(restored.served)
