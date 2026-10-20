"""Wiring the backend's external adapters into its ports, for chat and serve."""

import asyncio
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
    AgentSession,
    ClaudeCliRunner,
    ConfigError,
    GitCoderTree,
    ImportBlocked,
    McpHost,
    OpenedStore,
    OrbitConfig,
    RecoveryFailed,
    Role,
    SqliteStore,
    StorageSettings,
    StoreError,
    UsageApi,
    Verification,
    WorkspaceLock,
    WorkspaceOwner,
    git_top_level,
    in_git_work_tree,
    open_workspace_store,
    orbit_dir,
    read_config,
    read_storage,
    run_step,
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
    top = git_top_level(str(resolved))
    return Path(top) if top else resolved


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


class _MakeBackend(Protocol):
    """What builds the session a backend asks for, as :func:`_backend` does."""

    def __call__(
        self, backend: _Backend, directories: _Directories, *, pick_session: bool
    ) -> tuple[Callable[[], AgentSession], SqliteStore]: ...


@dataclass(frozen=True)
class _InProcess:
    """A backend set up in this process: what ``build``s its session, its ``store``, and the
    workspace ``lock``, with the repository's ``owner`` if another orbit owns it."""

    build: Callable[[], AgentSession]
    store: SqliteStore
    lock: WorkspaceLock
    owner: WorkspaceOwner | None


@contextmanager
def _in_process(
    flags: _Backend, directories: _Directories, *, pick_session: bool, make: _MakeBackend
) -> Iterator[_InProcess]:
    """Run the block with the backend ``flags`` ask for, made by ``make`` in ``directories``,
    holding the workspace lock if no other orbit does, and listing the saved sessions to pick
    from if ``pick_session``; yields the backend set up, then closes its store and lets the
    lock go.

    Exits with status 2 if the lock can't be made, a configuration is invalid or the store
    can't be opened.
    """
    with _repository_lock(directories.root) as (lock, owner):
        backend = replace(flags, owner=owner, lock=lock)
        build, store = make(backend, directories, pick_session=pick_session)
        try:
            yield _InProcess(build, store, lock, owner)
        finally:  # Whatever ran the session has closed it, so nothing uses the store any more.
            asyncio.run(store.close())


@contextmanager
def _repository_lock(root: Path) -> Iterator[tuple[WorkspaceLock, WorkspaceOwner | None]]:
    """Hold the workspace lock on the repository at ``root`` while the block runs, if no other
    orbit holds it; yields the lock and the owner, if another orbit is.

    Exits with status 2 if the lock can't be made.
    """
    lock = WorkspaceLock(orbit_dir(str(root)))
    with _exit_on(
        OSError, message=lambda exc: f"Couldn't lock the repository with {lock.path}: {exc}"
    ):
        owner = lock.take()
    try:
        yield lock, owner
    finally:
        lock.close()


def _backend(
    backend: _Backend, directories: _Directories, *, pick_session: bool
) -> tuple[Callable[[], AgentSession], SqliteStore]:
    """What builds the session ``backend`` asks for in ``directories``, listing the saved
    sessions to pick from if ``pick_session``, and the store.

    Exits with status 2 if a configuration is invalid or the store can't be opened.
    """
    root = directories.root
    config = _config()
    storage = _storage(directories.user_config)
    verification = _verification(config) if backend.verify else None
    agents = _agents(config, worktree=backend.worktree, review=backend.review)
    settings, pinned = _settings(backend.model, backend.tools)
    usage_source = UsageApi() if backend.usage else None
    opened = _open_store(directories.state, root)

    def build() -> AgentSession:
        return AgentSession(
            settings,
            model_choices=MODEL_ALIASES,
            verification=verification,
            agents=agents,
            workspace=str(root),
            check_at_startup=True,
            usage=usage_source,
            store=opened.store,
            pinned=pinned,
            pick_session=pick_session,
            name=backend.name,
            startup_notes=opened.notes,
            storage=storage,
            fallback=opened.fallback,
            owner=backend.owner,
            lock=backend.lock,
        )

    return build, opened.store


def _config() -> OrbitConfig:
    """The project file's configuration.

    Exits with status 2 if the file is invalid.
    """
    return _configured(read_config)


def _storage(config_dir: Path) -> StorageSettings:
    """The storage settings in the user config in ``config_dir``.

    Exits with status 2 if the file is invalid.
    """
    return _configured(lambda: read_storage(config_dir))


def _open_store(state: Path, root: Path) -> OpenedStore:
    """The store in the ``state`` directory for the workspace at ``root``, with the sessions an
    older orbit saved as files and the events an earlier run parked in fallback files imported,
    the notes on what was imported, and the fallback files.

    Exits with status 2 if the store can't be opened, the old sessions can't be imported or a
    fallback file can't be recovered.
    """
    with _exit_on(StoreError, ImportBlocked, RecoveryFailed):
        return asyncio.run(open_workspace_store(state, root))


def _verification(config: OrbitConfig) -> Verification:
    """What verifies tasks with ``config``'s steps, run as processes."""
    steps = config.verification.steps
    return Verification(steps, run_step, workspace_state, config.fallback_fixes)


def _settings(model: str | None, tools: ToolAccess | None) -> tuple[Settings, list[str]]:
    """The settings ``model`` and ``tools`` ask for, and the names of those given, which hold
    even in a session saved with other settings."""
    settings = Settings(model=model, tool_access=tools or "restricted")
    return settings, [name for name, given in (("model", model), ("tool_access", tools)) if given]


def _agents(config: OrbitConfig, *, worktree: bool, review: bool) -> Agents:
    """The agents that run tasks as ``config`` says, in the coder tree if ``worktree`` and
    the workspace is a git work tree, reviewing landed work if ``review``."""
    tree = GitCoderTree(".", config.setup) if worktree and in_git_work_tree(".") else None
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
        planner_role=_planner_role,
    )


def _planner_role() -> Role | ConfigError:
    """The planner's role as the project file says now, or why the file is invalid."""
    try:
        return read_config().roles[PLANNER]
    except ConfigError as exc:
        return exc
