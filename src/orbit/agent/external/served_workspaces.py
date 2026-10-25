"""Opening a workspace, locked, checked and configured off the event loop, then found or
registered in the store; and the workspaces a server serves as frontends add and remove them,
by their paths or as clones it makes, each opened so and flagged served, and purged if asked."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from orbit.agent.application.failures import log_unexpected
from orbit.agent.application.ports import Cloner
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.application.workspace_source import WorkspaceUnavailable
from orbit.agent.domain.config import OrbitConfig
from orbit.agent.domain.config_errors import INVALID_CONFIG, ConfigError
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.purging import Purged, PurgeRefused
from orbit.agent.domain.refusals import failed_to
from orbit.agent.domain.workspaces import PathKind
from orbit.agent.external.config_file import read_config
from orbit.agent.external.repository import (
    RepositoryUnreadable,
    git_top_level,
    has_git_identity,
    in_git_work_tree,
    orbit_dir,
)
from orbit.agent.external.session_import import ImportBlocked
from orbit.agent.external.shielding import shielded
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.store_startup import OpenedStore, open_workspace_store
from orbit.agent.external.workspace_lock import WorkspaceLock
from orbit.agent.external.workspace_purge import purge_losses, purge_workspace
from orbit.agent.external.workspace_registry import WorkspaceRegistry
from orbit.protocol import INTERNAL, WorkspaceCloning


@dataclass(frozen=True)
class ServedWorkspace:
    """A workspace a server serves: the one at ``root``, with the repository's ``owner`` if
    another orbit owns it, making it read-only, and its workspace ``lock``, ``kept`` if it was
    held already, so a failed add leaves it held."""

    root: Path
    owner: WorkspaceOwner | None
    lock: WorkspaceLock
    kept: bool = False

    def let_go(self) -> None:
        """Let go of the lock, unless it's kept."""
        if not self.kept:
            self.lock.close()


type HostMaker = Callable[[ServedWorkspace, OrbitConfig], Callable[[OpenedStore], WorkspaceHost]]
"""What gives, for a served workspace and its configuration, what builds its host over its
view of the store."""

type _Prepared = tuple[ServedWorkspace, Callable[[OpenedStore], WorkspaceHost]]


@dataclass(frozen=True)
class OpenedWorkspace:
    """A workspace opened over the store: the ``served`` workspace, with its lock held, the id
    it's registered with, and what ``make``s its host over its view of the store."""

    served: ServedWorkspace
    workspace_id: str
    make: Callable[[], WorkspaceHost]


@dataclass(frozen=True)
class Configuring:
    """How a workspace is configured as it's opened: from its file over the user config in
    ``config_dir``, once git is found to have an identity for commits there if the coder commits
    in a ``worktree``."""

    config_dir: Path
    worktree: bool


@dataclass(frozen=True)
class Opening:
    """How a workspace is opened: configured as ``configuring`` says, flagged served if
    ``flag``, its lock naming the server's ``url`` if it's known, and marked as a clone orbit
    made if ``managed``; the lock this server still ``held`` for it, if any, is kept rather than
    another taken."""

    configuring: Configuring
    flag: bool
    url: str | None = None
    managed: bool = False
    held: WorkspaceLock | None = None


async def open_workspace(
    store: SqliteStore, root: Path, make_host: HostMaker, opening: Opening
) -> OpenedWorkspace:
    """The workspace at ``root`` opened over ``store`` as ``opening`` says, its lock taken if
    it's free, configured, with its host made by ``make_host``. Raises
    :class:`WorkspaceUnavailable` if the lock can't be made, git has no identity for commits
    there, its configuration is invalid or the store fails, having let the lock go. Cancelled while
    it's prepared, it lets the lock go before the cancellation ends."""
    # Git, the configuration file and the lock would stall the event loop.
    preparing = asyncio.ensure_future(asyncio.to_thread(_prepared, root, make_host, opening))
    try:
        served, make = await shielded(preparing)
    except asyncio.CancelledError:
        # The thread ran on regardless and has finished: let go of the lock it took before the
        # cancellation ends.
        _let_go_prepared(preparing)
        raise
    try:
        opened = await _view(store, root, owned=served.owner is None, opening=opening)
    except BaseException:
        served.let_go()
        raise
    return OpenedWorkspace(served, opened.store.workspace_id, lambda: make(opened))


def _prepared(root: Path, make_host: HostMaker, opening: Opening) -> _Prepared:
    """The workspace at ``root``, its lock taken if it's free, checked and configured as
    ``opening`` says, with what ``make_host`` gives to build its host over its view of the
    store. Raises :class:`WorkspaceUnavailable` as :func:`open_workspace`
    says, and lets the lock go if anything after taking it fails."""
    served = _locked_served(root, opening)
    try:
        return served, make_host(served, workspace_config(root, opening.configuring))
    except BaseException:
        served.let_go()
        raise


def _locked_served(root: Path, opening: Opening) -> ServedWorkspace:
    """The workspace at ``root`` with the lock ``opening`` says is still held for it, kept, or
    else its own lock, taken if it's free. Raises :class:`WorkspaceUnavailable` as
    :func:`locked` says."""
    if opening.held is not None:
        return ServedWorkspace(root, None, opening.held, kept=True)
    lock, owner = locked(root, opening.url)
    return ServedWorkspace(root, owner, lock)


class ServedWorkspaces:
    """The workspaces a server serves from the ``store``, each with its host made by
    ``make_host``, configured as ``configuring`` says, and its workspace lock held, if it's
    free, until it's removed or :meth:`let_go`: those it starts with, as :meth:`served` serves
    them, and those frontends add, by their paths or as clones ``cloner`` makes, as a control
    plane's ``WorkspaceSource``; one added again at the same root while its lock is still held,
    as it is when unflagging it failed, keeps that lock, while another worktree of its
    repository takes a lock of its own. Once the server's ``url`` is known, the locks taken name
    it."""

    def __init__(
        self, store: SqliteStore, make_host: HostMaker, cloner: Cloner, *, configuring: Configuring
    ) -> None:
        self._store = store
        self._make_host = make_host
        self._cloner = cloner
        self._configuring = configuring
        self._locks: dict[str, WorkspaceLock] = {}
        self._roots: dict[str, Path] = {}
        self._fresh: list[str] = []
        self.url: str | None = None

    @property
    def locks(self) -> tuple[WorkspaceLock, ...]:
        """The locks of the workspaces served."""
        return tuple(self._locks.values())

    def served(
        self, root: Path, *, managed: bool = False
    ) -> tuple[ServedWorkspace, Callable[[], WorkspaceHost]]:
        """The workspace at ``root`` served from the start, marked as a clone orbit made if
        ``managed``, with what builds its host, its lock held, if it's free, left flagged as it
        was until :meth:`flag_served`. Raises :class:`WorkspaceUnavailable` as :meth:`open`
        says."""
        opening = Opening(self._configuring, flag=False, managed=managed)
        return asyncio.run(self._taken(root, opening))

    def cloned(self, url: str) -> str:
        """The path of a new clone of the repository at ``url``, made before serving starts and
        removed by :meth:`drop_clones` unless :meth:`keep_clones` is called first. Raises
        :class:`CloneFailed` if it fails."""
        path = asyncio.run(self.clone(url, _unshown))
        self._fresh.append(path)
        return path

    def keep_clones(self) -> None:
        """Keep the clones made before serving started, now that serving them has."""
        self._fresh.clear()

    def drop_clones(self) -> None:
        """Remove the clones made before serving started that aren't kept, their locks let go
        first."""
        for path in self._fresh:
            asyncio.run(self.remove(path))
        self._fresh.clear()

    def flag_served(self) -> None:
        """Flag every workspace served as served, so a server starting later serves them again.
        Raises :class:`WorkspaceUnavailable` if the store fails, having flagged those it flagged
        not served again."""
        asyncio.run(self._flag_served())

    async def _flag_served(self) -> None:
        """Flag every workspace served as served, as :meth:`flag_served` does."""
        flagged: list[str] = []
        try:
            for workspace_id in tuple(self._locks):
                await self.set_served(workspace_id, True)
                flagged.append(workspace_id)
        except WorkspaceUnavailable:
            for workspace_id in flagged:
                await self._unflag_anyway(workspace_id, self._roots[workspace_id])
            raise

    async def _unflag_anyway(self, workspace_id: str, root: Path) -> None:
        """Flag the workspace ``workspace_id``, at ``root``, not served again, as
        :func:`_unflag_anyway` does."""
        await _unflag_anyway(WorkspaceRegistry(self._store), workspace_id, root)

    def serves(self, workspace_id: str) -> bool:
        """Whether the workspace ``workspace_id`` is served."""
        return workspace_id in self._locks

    async def kind(self, path: str) -> PathKind:
        """What there is at ``path``."""
        return await asyncio.to_thread(_kind, Path(path))

    async def top_level(self, path: str) -> str:
        """The path of the workspace the directory at ``path`` stands for, as
        :func:`top_level` finds it."""
        return str(await asyncio.to_thread(top_level, Path(path).resolve()))

    async def clone(self, url: str, progress: Callable[[WorkspaceCloning], None]) -> str:
        """The path of a new clone of the repository at ``url``, as the cloner makes it."""
        return await self._cloner.clone(url, progress)

    async def remove(self, path: str) -> None:
        """Remove the clone at ``path``, as the cloner does."""
        await self._cloner.remove(path)

    async def open(self, root: str, *, managed: bool = False) -> WorkspaceHost:
        """The host of the workspace at ``root``, registered or found, flagged served and, if
        ``managed``, marked as a clone orbit made, its lock taken if it's free. Raises
        :class:`WorkspaceUnavailable` if git has no identity for the coder's commits there, its
        configuration is invalid, its lock can't be made or the store fails."""
        opening = Opening(self._configuring, flag=True, managed=managed)
        _, make = await self._taken(Path(root), opening)
        return make()

    async def _taken(
        self, root: Path, opening: Opening
    ) -> tuple[ServedWorkspace, Callable[[], WorkspaceHost]]:
        """The workspace at ``root``, opened as ``opening`` says, with what builds its host, its
        lock kept if it's held already, else taken if it's free, and held until it's removed or
        let go, naming the server's URL if it's known by then. Raises
        :class:`WorkspaceUnavailable` as :meth:`open` says, or as ``INTERNAL`` if the lock can't
        name the URL, having let it go."""
        opening = replace(opening, url=self.url, held=self._held(root))
        opened = await open_workspace(self._store, root, self._make_host, opening)
        # The server may have started listening while the workspace was opened.
        if self.url is not None and self.url != opening.url:
            await self._named(opened, self.url, flagged=opening.flag)
        self._locks[opened.workspace_id] = opened.served.lock
        self._roots[opened.workspace_id] = root
        return opened.served, opened.make

    def _held(self, root: Path) -> WorkspaceLock | None:
        """The lock still held for the workspace at ``root``, if any; worktrees of one
        repository share a lock file, but each takes its own lock, as only one holds it."""
        return next(
            (
                lock
                for workspace_id, lock in self._locks.items()
                if lock.held and self._roots[workspace_id] == root
            ),
            None,
        )

    async def _named(self, opened: OpenedWorkspace, url: str, *, flagged: bool) -> None:
        """Have the lock of the workspace ``opened`` name the server's ``url``. Raises
        :class:`WorkspaceUnavailable` as ``INTERNAL`` if it can't be written, having let the
        lock go and flagged the workspace not served again if it was ``flagged``."""
        lock = opened.served.lock
        try:
            lock.announce(url)
        except OSError as exc:
            opened.served.let_go()
            if flagged:
                await self._unflag_anyway(opened.workspace_id, opened.served.root)
            raise WorkspaceUnavailable(
                f"Couldn't write the workspace lock {lock.path}: {exc}", INTERNAL
            ) from None

    async def set_served(self, workspace_id: str, served: bool) -> None:
        """Flag the workspace ``workspace_id`` as ``served``, or not, in the store. Raises
        :class:`WorkspaceUnavailable` as ``INTERNAL`` if the store fails."""
        with _unavailable(StoreError):
            await WorkspaceRegistry(self._store).set_served(workspace_id, served)

    async def restore_served(self, workspace_id: str) -> None:
        """Flag the workspace ``workspace_id`` served again in the store, keeping when it was
        added. Raises :class:`WorkspaceUnavailable` as ``INTERNAL`` if the store fails."""
        with _unavailable(StoreError):
            await WorkspaceRegistry(self._store).restore_served(workspace_id)

    async def latest_added(self) -> str | None:
        """The path of the workspace served that was flagged served last, if any. Raises
        :class:`WorkspaceUnavailable` as ``INTERNAL`` if the store fails."""
        with _unavailable(StoreError):
            return await WorkspaceRegistry(self._store).latest_added()

    async def close(self, workspace_id: str) -> None:
        """Stop serving the workspace ``workspace_id`` and let go of its lock, leaving its flag
        as it is."""
        self._roots.pop(workspace_id)
        self._locks.pop(workspace_id).close()

    async def losses(self, workspace_id: str) -> list[str]:
        """What purging the workspace ``workspace_id`` would lose, as :func:`purge_losses`
        says. Raises :class:`WorkspaceUnavailable` as ``INTERNAL`` if the store fails."""
        with _unavailable(StoreError):
            workspace = await WorkspaceRegistry(self._store).workspace(workspace_id)
        return [] if workspace is None else await purge_losses(workspace)

    async def purge(self, workspace_id: str) -> Purged:
        """Delete the records of the workspace ``workspace_id``, as :func:`purge_workspace`
        does; returns what it deleted. Raises :class:`WorkspaceUnavailable` as ``INVALID`` if
        it's refused, or as ``INTERNAL`` if the store fails or a file can't be deleted."""
        try:
            with _unavailable(StoreError, OSError):
                return await purge_workspace(self._store, workspace_id)
        except PurgeRefused as exc:
            raise WorkspaceUnavailable(str(exc)) from None

    def let_go(self) -> None:
        """Let go of the locks of the workspaces still served."""
        for lock in self._locks.values():
            lock.close()
        self._locks.clear()
        self._roots.clear()


@contextmanager
def _unavailable(*errors: type[Exception]) -> Iterator[None]:
    """Run the block, raising :class:`WorkspaceUnavailable` as ``INTERNAL``, with its message,
    for any of the ``errors`` it raises."""
    try:
        yield
    except errors as exc:
        raise WorkspaceUnavailable(str(exc), INTERNAL) from None


def _unshown(progress: WorkspaceCloning) -> None:
    """Show no one a clone's ``progress``."""


def _let_go_prepared(preparing: asyncio.Future[_Prepared]) -> None:
    """Let go of the lock of the workspace ``preparing`` prepared for an add that was cancelled
    meanwhile, if it was prepared."""
    if preparing.cancelled() or preparing.exception() is not None:
        return
    served, _ = preparing.result()
    served.let_go()


def top_level(directory: Path) -> Path:
    """The project directory ``directory``, resolved, stands for: its git work tree's top
    level, if it's in one."""
    top = git_top_level(str(directory))
    return Path(top) if top else directory


def _kind(path: Path) -> PathKind:
    """What there is at ``path``."""
    if path.is_dir():
        return PathKind.DIRECTORY
    return PathKind.FILE if path.exists() else PathKind.MISSING


def workspace_config(root: Path, configuring: Configuring) -> OrbitConfig:
    """The configuration of the workspace at ``root``, as ``configuring`` says. Raises
    :class:`WorkspaceUnavailable` if it or the user config is invalid, or, with a worktree, git
    has no identity for commits there."""
    problem = _identity_problem(root) if configuring.worktree else None
    if problem is not None:
        raise WorkspaceUnavailable(problem)
    try:
        return read_config(str(root), configuring.config_dir)
    except ConfigError as exc:
        raise WorkspaceUnavailable(f"{INVALID_CONFIG}{exc}") from None


def _identity_problem(root: Path) -> str | None:
    """What's wrong if the workspace at ``root`` is a git repository that git has no identity
    for commits in, as the coder's worktree commits there; none otherwise."""
    if not in_git_work_tree(str(root)) or has_git_identity(str(root)):
        return None
    return (
        f"Git has no identity for commits in {root}: set user.name and user.email"
        ' (git config --global user.name "…"), or mount your ~/.gitconfig into the container.'
    )


def locked(root: Path, url: str | None = None) -> tuple[WorkspaceLock, WorkspaceOwner | None]:
    """The workspace lock on the repository at ``root``, naming the server's ``url`` if it's
    known, and the owner, if another orbit holds it. Raises :class:`WorkspaceUnavailable` as
    ``INTERNAL`` if it can't be found or made."""
    lock = WorkspaceLock(_orbit_dir(root))
    if url is not None:
        lock.announce(url)
    try:
        return lock, lock.take()
    except OSError as exc:
        raise WorkspaceUnavailable(
            f"Couldn't lock the repository with {lock.path}: {exc}", INTERNAL
        ) from None


def _orbit_dir(root: Path) -> Path:
    """Orbit's directory for the workspace at ``root``, where its lock is. Raises
    :class:`WorkspaceUnavailable` as ``INTERNAL`` if git can't find it in its repository."""
    try:
        return orbit_dir(str(root))
    except RepositoryUnreadable as exc:
        raise WorkspaceUnavailable(str(exc), INTERNAL) from None


async def _view(store: SqliteStore, root: Path, *, owned: bool, opening: Opening) -> OpenedStore:
    """The workspace at ``root``'s view of ``store``, as :func:`open_workspace_store` opens it,
    recovering its fallback files if this orbit ``owned`` it, marked managed and flagged as
    served as ``opening`` says. Raises :class:`WorkspaceUnavailable` as ``INTERNAL`` if that
    fails, as it may have changed the store or the workspace's files by then."""
    with _unavailable(StoreError, RepositoryUnreadable, ImportBlocked, RecoveryFailed):
        opened = await open_workspace_store(store, root, owned=owned)
        workspace_id = opened.store.workspace_id
        registry = WorkspaceRegistry(store)
        if opening.managed:
            await registry.set_managed(workspace_id)
        registered = await registry.workspace(workspace_id)
        # Flagged last, so a later failure can't leave served a workspace whose add failed.
        if opening.flag:
            await _flagged(registry, workspace_id, root)
    return replace(opened, managed=registered is not None and registered.managed)


async def _flagged(registry: WorkspaceRegistry, workspace_id: str, root: Path) -> None:
    """Flag the workspace ``workspace_id``, at ``root``, served in ``registry``; cancelled
    meanwhile, flag it not served again, so a server starting later doesn't serve a workspace
    whose add was cancelled."""
    try:
        await registry.set_served(workspace_id, True)
    except asyncio.CancelledError:
        # The store's one thread runs writes in order, so this lands after the flag, if it does.
        await _unflag_anyway(registry, workspace_id, root)
        raise


async def _unflag_anyway(registry: WorkspaceRegistry, workspace_id: str, root: Path) -> None:
    """Flag the workspace ``workspace_id``, at ``root``, not served again in ``registry``, as
    it cleans up after another failure, which it mustn't replace: its own failure is only
    logged."""
    try:
        await registry.set_served(workspace_id, False)
    except StoreError as exc:
        log_unexpected(failed_to(f"flag {root} not served", exc), exc)
