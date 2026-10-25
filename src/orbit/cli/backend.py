"""Wiring the backend's external adapters into its ports, building the hosts chat and serve
share."""

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
    ConfigError,
    Configuring,
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
    SystemTempDirs,
    UsageApi,
    Verification,
    WorkspaceHost,
    WorkspaceLock,
    WorkspaceOwner,
    WorkspaceUnavailable,
    file_names,
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
from orbit.cli.runtimes import build_runtimes
from orbit.protocol import PLANNER, Settings, ToolAccess
from orbit.telemetry import AgentCollector
from orbit.tracing import Gauges, NoGauges, NoTelemetry, Telemetry

_NO_TELEMETRY = NoTelemetry()
_NO_GAUGES = NoGauges()
_NO_COLLECTOR = AgentCollector()


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
    ``usage`` limits; a ``new`` session, perhaps with a ``name``; the ``runtime``, if any, of
    every role that doesn't name its own; the ``owner`` of the repository, if another orbit
    owns it, making this one read-only, with the workspace ``lock`` it tries until it's free;
    the ``telemetry`` its work is traced with, the ``gauges`` its control plane reports live
    values through, and the ``collector`` its agents send their own telemetry to."""

    model: str | None
    tools: ToolAccess | None
    verify: bool
    worktree: bool
    review: bool
    usage: bool
    new: bool
    name: str | None
    runtime: str | None = None
    owner: WorkspaceOwner | None = None
    lock: WorkspaceLock | None = None
    telemetry: Telemetry = _NO_TELEMETRY
    gauges: Gauges = _NO_GAUGES
    collector: AgentCollector = _NO_COLLECTOR

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
class _UserConfig:
    """The user config in ``directory``, with the ``storage`` settings it set at startup; the
    role fields it sets are read again as each workspace is configured."""

    directory: Path
    storage: StorageSettings


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
    """Run the block with the backend ``flags`` ask for, set up by ``make`` in ``directories``,
    holding the workspace lock if no other orbit does, and listing the saved sessions to pick
    from if ``pick_session``; yields the backend set up, then closes its store and lets the lock
    go.

    Exits with status 2 if the workspace can't be opened.
    """
    # Every adapter is given the workspace's root, as under orbit serve, which serves several
    # from one process, so the current directory is left as it is.
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
    user = _user_config(directories.user_config)
    usage_source = _usage_source(flags)
    make = _maker(flags, user, pick_session=pick_session)
    store = _open(directories.state)
    configuring = Configuring(user.directory, worktree=False, runtime=flags.runtime)
    try:
        opened = _opened(store, directories.root, make, configuring)
    except BaseException:
        asyncio.run(store.close())
        raise
    served = opened.served
    return _InProcess(
        lambda: ControlPlane(
            [opened.make()],
            usage_source,
            ledger=store,
            telemetry=flags.telemetry,
            gauges=flags.gauges,
        ),
        store,
        opened.workspace_id,
        served.lock,
        served.owner,
    )


def _opened(
    store: SqliteStore, root: Path, make: HostMaker, configuring: Configuring
) -> OpenedWorkspace:
    """The workspace at ``root`` opened over ``store`` for a chat, configured as
    ``configuring`` says, its host made by ``make``.

    Exits with status 2 if it can't be opened, as when a runtime its roles run on isn't on
    PATH.
    """
    with _exit_on(WorkspaceUnavailable):
        opening = Opening(configuring, flag=False)
        return asyncio.run(open_workspace(store, root, make, opening))


def _maker(flags: _Backend, user: _UserConfig, *, pick_session: bool) -> HostMaker:
    """What gives, for a served workspace and its configuration, what builds its host as
    ``flags`` ask, with the ``user`` config, listing the saved sessions to pick from if
    ``pick_session``."""

    def make(
        workspace: ServedWorkspace, config: OrbitConfig
    ) -> Callable[[OpenedStore], WorkspaceHost]:
        backend = replace(flags, owner=workspace.owner, lock=workspace.lock)
        return _host_maker(backend, workspace.root, config, user, pick_session=pick_session)

    return make


def _host_maker(
    backend: _Backend,
    root: Path,
    config: OrbitConfig,
    user: _UserConfig,
    *,
    pick_session: bool,
) -> Callable[[OpenedStore], WorkspaceHost]:
    """What builds the host of the workspace at ``root``, configured as ``config`` says, its
    notices among its startup notes, over its view of the store, as ``backend`` asks, keeping
    its sessions as the ``user`` config's storage settings say and reading its planner's role
    again over it, listing the saved sessions to pick from if ``pick_session``."""
    verification = _verification(config) if backend.verify else None
    agents = _agents(config, root, backend, user.directory)
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
            startup_notes=(*config.notices, *opened.notes),
            storage=user.storage,
            fallback=opened.fallback,
            recover=opened.recover,
            owner=backend.owner,
            lock=backend.lock,
            managed=opened.managed,
            ledger=opened.store.ledger,
            telemetry=backend.telemetry,
        )

    return make


def _usage_source(flags: _Backend) -> UsageApi | None:
    """What fetches the usage limits, if ``flags`` ask to show them."""
    return UsageApi() if flags.usage else None


def _user_config(config_dir: Path) -> _UserConfig:
    """The user config in ``config_dir``, with the storage settings it sets.

    Exits with status 2 if the file is invalid.
    """
    return _UserConfig(config_dir, _configured(lambda: read_storage(config_dir)))


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


def _agents(config: OrbitConfig, root: Path, backend: _Backend, config_dir: Path) -> Agents:
    """The agents that run tasks as ``config`` says in the workspace at ``root``, on the
    runtimes its enabled roles run on, in the coder tree if ``backend`` asks for a worktree and
    the workspace is a git work tree, reviewing landed work if it asks to, sending their own
    telemetry to its collector, and reading the planner's role again over the user config in
    ``config_dir``, with its runtime.

    Raises :class:`WorkspaceUnavailable` if a runtime the enabled roles run on isn't on PATH.
    """
    where = str(root)
    worktree = backend.worktree and in_git_work_tree(where)
    tree = GitCoderTree(where, config.setup) if worktree else None
    runtime = backend.runtime
    return Agents(
        McpHost(),
        build_runtimes(config.roles, backend.collector),
        secrets.token_urlsafe,
        config.roles,
        config.limits,
        tree=tree,
        review=backend.review,
        review_limits=config.review_limits,
        write_config=write_checks,
        planner_role=lambda: _planner_role(where, config_dir, runtime),
        file_names=file_names,
        root=where,
        temp_dirs=SystemTempDirs(),
        personal=config.personal,
    )


def _planner_role(root: str, config_dir: Path, runtime: str | None) -> Role | ConfigError:
    """The planner's role as the project file of the workspace at ``root`` and the user config
    in ``config_dir`` say now, with the run's ``runtime``, if any, or why either is invalid."""
    try:
        return read_config(root, config_dir, runtime).roles[PLANNER]
    except ConfigError as exc:
        return exc
