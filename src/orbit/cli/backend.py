"""Wiring the backend's external adapters into its ports, building the hosts chat and serve
share."""

import asyncio
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import typer

from orbit.agent import (
    MODEL_ALIASES,
    Agents,
    ClaudeCliRunner,
    ConfigError,
    ControlPlane,
    GitCoderTree,
    HostMaker,
    McpHost,
    OpenedStore,
    OpenedWorkspace,
    Opening,
    OrbitConfig,
    Role,
    ServedWorkspace,
    SqliteStore,
    StorageSettings,
    StoreError,
    UsageApi,
    Verification,
    WorkspaceHost,
    WorkspaceLock,
    WorkspaceOwner,
    WorkspaceUnavailable,
    in_git_work_tree,
    open_workspace,
    read_config,
    read_storage,
    run_step,
    top_level,
    workspace_state,
    write_checks,
)
from orbit.cli.errors import _configured, _exit_on
from orbit.protocol import PLANNER, Settings, ToolAccess


def _workspace(path: Path) -> Path:
    """The project directory at ``path``: its git work tree's top level, if it's in one.

    Exits with status 2 if ``path`` doesn't exist or isn't a directory.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        typer.echo(f"Workspace {resolved} doesn't exist.", err=True)
        raise typer.Exit(2)
    if not resolved.is_dir():
        typer.echo(f"Workspace {resolved} isn't a directory.", err=True)
        raise typer.Exit(2)
    return top_level(resolved)


@dataclass(frozen=True)
class _Backend:
    """What the backend flags ask for: the planner's ``model``, new tasks' ``tools``, and
    whether to ``verify``, run tasks in a ``worktree``, ``review`` landed work and show the
    ``usage`` limits; a ``new`` session, perhaps with a ``name``; and the ``owner`` of the
    repository, if another orbit owns it, making this one read-only, with the workspace
    ``lock`` it tries until it's free."""

    model: str | None
    tools: ToolAccess | None
    verify: bool
    worktree: bool
    review: bool
    usage: bool
    new: bool
    name: str | None
    owner: WorkspaceOwner | None = None
    lock: WorkspaceLock | None = None

    @property
    def fresh(self) -> bool:
        """Whether a new session was asked for."""
        return self.new or self.name is not None


@dataclass(frozen=True)
class _Directories:
    """Where a backend works: the workspace at ``root``, with the store in ``state`` and the
    user config in ``user_config``."""

    root: Path
    state: Path
    user_config: Path


@dataclass(frozen=True)
class _InProcess:
    """A backend set up in this process: what ``build``s its control plane, its ``store``, the
    id of the workspace it serves, and the workspace ``lock``, with the repository's ``owner``
    if another orbit owns it."""

    build: Callable[[], ControlPlane]
    store: SqliteStore
    workspace_id: str
    lock: WorkspaceLock
    owner: WorkspaceOwner | None


class _MakeBackend(Protocol):
    """What sets up the backend a chat asks for, as :func:`_backend` does."""

    def __call__(
        self, flags: _Backend, directories: _Directories, *, pick_session: bool
    ) -> _InProcess: ...


@contextmanager
def _in_process(
    flags: _Backend, directories: _Directories, *, pick_session: bool, make: _MakeBackend
) -> Iterator[_InProcess]:
    """Run the block in the workspace, with the backend ``flags`` ask for, set up by ``make``
    in ``directories``, holding the workspace lock if no other orbit does, and listing the
    saved sessions to pick from if ``pick_session``; yields the backend set up, then closes its
    store and lets the lock go.

    Exits with status 2 if the workspace can't be opened.
    """
    # Every adapter works relative to the current directory, so move into the workspace
    # before anything reads the filesystem.
    os.chdir(directories.root)
    backend = make(flags, directories, pick_session=pick_session)
    try:
        yield backend
    finally:  # Whatever ran the session has closed it, so nothing uses the store any more.
        try:
            asyncio.run(backend.store.close())
        finally:
            backend.lock.close()


def _backend(flags: _Backend, directories: _Directories, *, pick_session: bool) -> _InProcess:
    """The backend ``flags`` ask for over the workspace in ``directories``, opened as
    :func:`open_workspace` opens it, without checking git's identity, as only ``orbit serve``
    does, nor flagging it served, listing the saved sessions to pick from if ``pick_session``.

    Exits with status 2 if a configuration is invalid or the workspace can't be opened.
    """
    storage = _storage(directories.user_config)
    usage_source = _usage_source(flags)
    make = _maker(flags, storage, pick_session=pick_session)
    store = _open(directories.state)
    try:
        opened = _opened(store, directories.root, make)
    except BaseException:
        asyncio.run(store.close())
        raise
    served = opened.served
    return _InProcess(
        lambda: ControlPlane([opened.make()], usage_source),
        store,
        opened.workspace_id,
        served.lock,
        served.owner,
    )


def _opened(store: SqliteStore, root: Path, make: HostMaker) -> OpenedWorkspace:
    """The workspace at ``root`` opened over ``store`` for a chat, its host made by ``make``.

    Exits with status 2 if it can't be opened.
    """
    with _exit_on(WorkspaceUnavailable):
        opening = Opening(identity=False, flag=False)
        return asyncio.run(open_workspace(store, root, make, opening))


def _maker(flags: _Backend, storage: StorageSettings, *, pick_session: bool) -> HostMaker:
    """What gives, for a served workspace and its configuration, what builds its host as
    ``flags`` ask, keeping its sessions as ``storage`` says, listing the saved sessions to pick
    from if ``pick_session``."""

    def make(
        workspace: ServedWorkspace, config: OrbitConfig
    ) -> Callable[[OpenedStore], WorkspaceHost]:
        backend = replace(flags, owner=workspace.owner, lock=workspace.lock)
        return _host_maker(backend, workspace.root, config, storage, pick_session=pick_session)

    return make


def _host_maker(
    backend: _Backend,
    root: Path,
    config: OrbitConfig,
    storage: StorageSettings,
    *,
    pick_session: bool,
) -> Callable[[OpenedStore], WorkspaceHost]:
    """What builds the host of the workspace at ``root``, configured as ``config`` says, over
    its view of the store, as ``backend`` asks, keeping its sessions as ``storage`` says,
    listing the saved sessions to pick from if ``pick_session``."""
    verification = _verification(config) if backend.verify else None
    agents = _agents(config, root, worktree=backend.worktree, review=backend.review)
    settings, pinned = _settings(backend.model, backend.tools)

    def make(opened: OpenedStore) -> WorkspaceHost:
        return WorkspaceHost(
            settings,
            model_choices=MODEL_ALIASES,
            verification=verification,
            agents=agents,
            workspace=str(root),
            check_at_startup=True,
            workspace_id=opened.store.workspace_id,
            store=opened.store,
            pinned=pinned,
            pick_session=pick_session,
            name=backend.name,
            startup_notes=opened.notes,
            storage=storage,
            fallback=opened.fallback,
            recover=opened.recover,
            owner=backend.owner,
            lock=backend.lock,
        )

    return make


def _usage_source(flags: _Backend) -> UsageApi | None:
    """What fetches the usage limits, if ``flags`` ask to show them."""
    return UsageApi() if flags.usage else None


def _storage(config_dir: Path) -> StorageSettings:
    """The storage settings in the user config in ``config_dir``.

    Exits with status 2 if the file is invalid.
    """
    return _configured(lambda: read_storage(config_dir))


def _open(state: Path) -> SqliteStore:
    """The store in the ``state`` directory.

    Exits with status 2 if it can't be opened.
    """
    with _exit_on(StoreError):
        return asyncio.run(SqliteStore.open(state))


def _verification(config: OrbitConfig) -> Verification:
    """What verifies tasks with ``config``'s steps, run as processes."""
    steps = config.verification.steps
    return Verification(steps, run_step, workspace_state, config.fallback_fixes)


def _settings(model: str | None, tools: ToolAccess | None) -> tuple[Settings, list[str]]:
    """The settings ``model`` and ``tools`` ask for, and the names of those given, which hold
    even in a session saved with other settings."""
    settings = Settings(model=model, tool_access=tools or "restricted")
    return settings, [name for name, given in (("model", model), ("tool_access", tools)) if given]


def _agents(config: OrbitConfig, root: Path, *, worktree: bool, review: bool) -> Agents:
    """The agents that run tasks as ``config`` says in the workspace at ``root``, in the coder
    tree if ``worktree`` and the workspace is a git work tree, reviewing landed work if
    ``review``."""
    where = str(root)
    tree = GitCoderTree(where, config.setup) if worktree and in_git_work_tree(where) else None
    return Agents(
        McpHost(),
        ClaudeCliRunner(),
        secrets.token_urlsafe,
        config.roles,
        config.limits,
        tree=tree,
        review=review,
        review_limits=config.review_limits,
        write_config=write_checks,
        planner_role=lambda: _planner_role(where),
        root=where,
    )


def _planner_role(root: str) -> Role | ConfigError:
    """The planner's role as the project file of the workspace at ``root`` says now, or why the
    file is invalid."""
    try:
        return read_config(root).roles[PLANNER]
    except ConfigError as exc:
        return exc
