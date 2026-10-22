"""Wiring the backend's external adapters into its ports, for chat and serve."""

import asyncio
import os
import secrets
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
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
    ImportBlocked,
    McpHost,
    OpenedStore,
    OrbitConfig,
    RecoveryFailed,
    Role,
    ServedWorkspace,
    ServedWorkspaces,
    SqliteStore,
    StorageSettings,
    StoreError,
    UsageApi,
    Verification,
    WorkspaceHost,
    WorkspaceLock,
    WorkspaceOwner,
    WorkspaceRegistry,
    WorkspaceUnavailable,
    in_git_work_tree,
    locked,
    open_workspace_store,
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
class _Made:
    """What a backend's wiring made: what ``build``s its control plane, the ``store`` it opened,
    and the id of the workspace it serves."""

    build: Callable[[], ControlPlane]
    store: SqliteStore
    workspace_id: str


class _MakeBackend(Protocol):
    """What builds the control plane a backend asks for, as :func:`_backend` does."""

    def __call__(
        self, backend: _Backend, directories: _Directories, *, pick_session: bool
    ) -> _Made: ...


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


@contextmanager
def _in_process(
    flags: _Backend, directories: _Directories, *, pick_session: bool, make: _MakeBackend
) -> Iterator[_InProcess]:
    """Run the block in the workspace, with the backend ``flags`` ask for, made by ``make`` in
    ``directories``, holding the workspace lock if no other orbit does, and listing the saved
    sessions to pick from if ``pick_session``; yields the backend set up, then closes its store
    and lets the lock go.

    Exits with status 2 if the lock can't be made, a configuration is invalid or the store
    can't be opened.
    """
    # Every adapter works relative to the current directory, so move into the workspace
    # before anything reads the filesystem.
    os.chdir(directories.root)
    with _repository_lock(directories.root) as (lock, owner):
        backend = replace(flags, owner=owner, lock=lock)
        made = make(backend, directories, pick_session=pick_session)
        try:
            yield _InProcess(made.build, made.store, made.workspace_id, lock, owner)
        finally:  # Whatever ran the session has closed it, so nothing uses the store any more.
            asyncio.run(made.store.close())


@contextmanager
def _repository_lock(root: Path) -> Iterator[tuple[WorkspaceLock, WorkspaceOwner | None]]:
    """Hold the workspace lock on the repository at ``root`` while the block runs, if no other
    orbit holds it; yields the lock and the owner, if another orbit is.

    Exits with status 2 if the lock can't be made.
    """
    with _exit_on(WorkspaceUnavailable):
        lock, owner = locked(root)
    try:
        yield lock, owner
    finally:
        lock.close()


def _backend(backend: _Backend, directories: _Directories, *, pick_session: bool) -> _Made:
    """What builds the control plane over the workspace host ``backend`` asks for in
    ``directories``, listing the saved sessions to pick from if ``pick_session``, with the store
    it opened once and the workspace's view of it.

    Exits with status 2 if a configuration is invalid or the store can't be opened.
    """
    storage = _storage(directories.user_config)
    root = directories.root
    make = _host_maker(backend, root, _config(root), storage, pick_session=pick_session)
    usage_source = UsageApi() if backend.usage else None
    store, opened = _open_store(directories.state, root, owned=backend.owner is None)
    return _Made(
        lambda: ControlPlane([make(opened)], usage_source), store, opened.store.workspace_id
    )


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


def _workspaces(
    stack: ExitStack, flags: _Backend, store: SqliteStore, storage: StorageSettings
) -> ServedWorkspaces:
    """The workspaces ``orbit serve`` serves from the ``store``, each with its host made as
    ``flags`` ask, keeping its sessions as ``storage`` says, and its lock held until it's
    removed or ``stack`` closes."""

    def make(
        workspace: ServedWorkspace, config: OrbitConfig
    ) -> Callable[[OpenedStore], WorkspaceHost]:
        backend = replace(flags, owner=workspace.owner, lock=workspace.lock)
        return _host_maker(backend, workspace.root, config, storage, pick_session=False)

    return ServedWorkspaces(stack, store, make, worktree=flags.worktree)


@dataclass(frozen=True)
class _Serving:
    """What ``orbit serve`` serves: what ``build``s its control plane, the ``workspaces`` it
    starts serving, and what's ``served`` as frontends add and remove workspaces."""

    build: Callable[[], ControlPlane]
    workspaces: tuple[ServedWorkspace, ...]
    served: ServedWorkspaces


@contextmanager
def _serving(
    flags: _Backend, added: Sequence[Path], state: Path, user_config: Path
) -> Iterator[_Serving]:
    """Run the block serving the ``added`` workspaces, flagged as served in the store in
    ``state`` once each is found to be servable, and those flagged before, with the backend
    ``flags`` ask for and the user config in ``user_config``, holding each one's workspace lock
    if no other orbit does; yields what's served, then lets the locks go and closes the store.
    A workspace flagged before whose directory is gone, or that can't be served, is skipped,
    with a warning.

    Exits with status 2 if the store can't be opened, or an ``added`` workspace can't be
    registered, locked or opened, git has no identity for its repository with the coder
    committing there, or its configuration is invalid.
    """
    storage = _storage(user_config)
    usage_source = UsageApi() if flags.usage else None
    with ExitStack() as stack:
        store = _open(state)
        stack.callback(lambda: asyncio.run(store.close()))
        source = _workspaces(stack, flags, store, storage)
        served = [_added(source, root) for root in dict.fromkeys(added)]
        served += _earlier(source, store)
        workspaces = tuple(workspace for workspace, _ in served)
        makers = [make for _, make in served]

        def build() -> ControlPlane:
            return ControlPlane([make() for make in makers], usage_source, source)

        yield _Serving(build, workspaces, source)


def _added(
    source: ServedWorkspaces, root: Path
) -> tuple[ServedWorkspace, Callable[[], WorkspaceHost]]:
    """The workspace at ``root``, added with ``--add``, served by ``source`` and flagged served.

    Exits with status 2 if it can't be served, leaving it unflagged.
    """
    with _exit_on(WorkspaceUnavailable):
        return source.served(root)


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


def _config(root: Path) -> OrbitConfig:
    """The configuration in the project file of the workspace at ``root``.

    Exits with status 2 if the file is invalid.
    """
    return _configured(lambda: read_config(str(root)))


def _storage(config_dir: Path) -> StorageSettings:
    """The storage settings in the user config in ``config_dir``.

    Exits with status 2 if the file is invalid.
    """
    return _configured(lambda: read_storage(config_dir))


def _open_store(state: Path, root: Path, *, owned: bool) -> tuple[SqliteStore, OpenedStore]:
    """The store in the ``state`` directory and the workspace at ``root``'s view of it, with the
    sessions an older orbit saved as files imported and, if this orbit ``owned`` the workspace,
    the events an earlier run parked in fallback files too, the notes on what was imported, and
    the fallback files.

    Exits with status 2 if the store can't be opened, the old sessions can't be imported or a
    fallback file can't be recovered.
    """
    store = _open(state)
    try:
        return store, _open_view(store, root, owned=owned)
    except BaseException:
        asyncio.run(store.close())
        raise


def _open(state: Path) -> SqliteStore:
    """The store in the ``state`` directory.

    Exits with status 2 if it can't be opened.
    """
    with _exit_on(StoreError):
        return asyncio.run(SqliteStore.open(state))


def _open_view(store: SqliteStore, root: Path, *, owned: bool) -> OpenedStore:
    """The workspace at ``root``'s view of ``store``, as :func:`open_workspace_store` opens it,
    recovering its fallback files if this orbit ``owned`` it.

    Exits with status 2 if it can't be registered, its old sessions can't be imported or a
    fallback file can't be recovered.
    """
    with _exit_on(StoreError, ImportBlocked, RecoveryFailed):
        return asyncio.run(open_workspace_store(store, root, owned=owned))


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
