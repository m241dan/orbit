"""Opening a workspace, locked, checked and configured off the event loop, then found or
registered in the store; and the workspaces a server serves as frontends add and remove them,
each opened so and flagged served."""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from orbit.agent.application.control_plane import WorkspaceUnavailable
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.config import OrbitConfig
from orbit.agent.domain.config_errors import INVALID_CONFIG, ConfigError
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.workspaces import PathKind
from orbit.agent.external.config_file import read_config
from orbit.agent.external.repository import (
    git_top_level,
    has_git_identity,
    in_git_work_tree,
    orbit_dir,
)
from orbit.agent.external.session_import import ImportBlocked
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore
from orbit.agent.external.store_startup import OpenedStore, open_workspace_store
from orbit.agent.external.workspace_lock import WorkspaceLock
from orbit.agent.external.workspace_registry import WorkspaceRegistry
from orbit.protocol import INTERNAL


@dataclass(frozen=True)
class ServedWorkspace:
    """A workspace a server serves: the one at ``root``, with the repository's ``owner`` if
    another orbit owns it, making it read-only, and its workspace ``lock``."""

    root: Path
    owner: WorkspaceOwner | None
    lock: WorkspaceLock


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
class Opening:
    """How a workspace is opened: checked for a git identity if ``identity``, flagged served if
    ``flag``, and its lock naming the server's ``url`` if it's known."""

    identity: bool
    flag: bool
    url: str | None = None


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
        served, make = await asyncio.shield(preparing)
    except asyncio.CancelledError:
        preparing.add_done_callback(_let_go_prepared)
        # The thread runs on regardless; waiting for it means the lock it takes is let go by the
        # time the cancellation is, as that callback runs before this wait ends.
        await asyncio.wait([preparing])
        raise
    try:
        opened = await _view(store, root, owned=served.owner is None, flag=opening.flag)
    except BaseException:
        served.lock.close()
        raise
    return OpenedWorkspace(served, opened.store.workspace_id, lambda: make(opened))


def _prepared(root: Path, make_host: HostMaker, opening: Opening) -> _Prepared:
    """The workspace at ``root``, its lock taken if it's free, checked and configured as
    ``opening`` says, with what ``make_host`` gives to build its host over its view of the
    store. Raises :class:`WorkspaceUnavailable` as :func:`open_workspace`
    says, and lets the lock go if anything after taking it fails."""
    lock, owner = locked(root, opening.url)
    served = ServedWorkspace(root, owner, lock)
    try:
        return served, make_host(served, workspace_config(root, worktree=opening.identity))
    except BaseException:
        lock.close()
        raise


class ServedWorkspaces:
    """The workspaces a server serves from the ``store``, each with its host made by
    ``make_host``, checked for a git identity if the coder works in a ``worktree``, and its
    workspace lock held, if it's free, until it's removed or ``stack`` closes: those it starts
    with, as :meth:`served` serves them, and those frontends add, as a control plane's
    ``WorkspaceSource``. Once the server's ``url`` is known, the locks taken name it."""

    def __init__(
        self,
        stack: ExitStack,
        store: SqliteStore,
        make_host: HostMaker,
        *,
        worktree: bool,
    ) -> None:
        self._store = store
        self._make_host = make_host
        self._worktree = worktree
        self._locks: dict[str, WorkspaceLock] = {}
        self.url: str | None = None
        stack.callback(self._let_go)

    @property
    def locks(self) -> tuple[WorkspaceLock, ...]:
        """The locks of the workspaces served."""
        return tuple(self._locks.values())

    def served(self, root: Path) -> tuple[ServedWorkspace, Callable[[], WorkspaceHost]]:
        """The workspace at ``root`` served from the start, with what builds its host, its lock
        held, if it's free, left flagged as it was until :meth:`flag_served`. Raises
        :class:`WorkspaceUnavailable` as :meth:`open` says."""
        return asyncio.run(self._taken(root, flag=False))

    def flag_served(self) -> None:
        """Flag every workspace served as served, so a server starting later serves them again.
        Raises :class:`WorkspaceUnavailable` if the store fails."""
        asyncio.run(self._flag_served())

    async def _flag_served(self) -> None:
        """Flag every workspace served as served, as :meth:`flag_served` does."""
        for workspace_id in tuple(self._locks):
            await self.set_served(workspace_id, True)

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

    async def open(self, root: str) -> WorkspaceHost:
        """The host of the workspace at ``root``, registered or found and flagged served, its
        lock taken if it's free. Raises :class:`WorkspaceUnavailable` if git has no identity for
        the coder's commits there, its configuration is invalid, its lock can't be made or the
        store fails."""
        _, make = await self._taken(Path(root), flag=True)
        return make()

    async def _taken(
        self, root: Path, *, flag: bool
    ) -> tuple[ServedWorkspace, Callable[[], WorkspaceHost]]:
        """The workspace at ``root``, registered or found and flagged served if ``flag``, with
        what builds its host, its lock taken if it's free and held until it's removed or the
        stack closes, naming the server's URL if it's known by then. Raises
        :class:`WorkspaceUnavailable` as :meth:`open` says, or as ``INTERNAL`` if the lock can't
        name the URL, having let it go."""
        opening = Opening(identity=self._worktree, flag=flag, url=self.url)
        opened = await open_workspace(self._store, root, self._make_host, opening)
        # The server may have started listening while the workspace was opened.
        if self.url is not None and self.url != opening.url:
            await self._named(opened, self.url, flagged=flag)
        self._locks[opened.workspace_id] = opened.served.lock
        return opened.served, opened.make

    async def _named(self, opened: OpenedWorkspace, url: str, *, flagged: bool) -> None:
        """Have the lock of the workspace ``opened`` name the server's ``url``. Raises
        :class:`WorkspaceUnavailable` as ``INTERNAL`` if it can't be written, having let the
        lock go and flagged the workspace not served again if it was ``flagged``."""
        lock = opened.served.lock
        try:
            lock.announce(url)
        except OSError as exc:
            lock.close()
            if flagged:
                await self.set_served(opened.workspace_id, False)
            raise WorkspaceUnavailable(
                f"Couldn't write the workspace lock {lock.path}: {exc}", INTERNAL
            ) from None

    async def set_served(self, workspace_id: str, served: bool) -> None:
        """Flag the workspace ``workspace_id`` as ``served``, or not, in the store. Raises
        :class:`WorkspaceUnavailable` as ``INTERNAL`` if the store fails."""
        try:
            await WorkspaceRegistry(self._store).set_served(workspace_id, served)
        except StoreError as exc:
            raise WorkspaceUnavailable(str(exc), INTERNAL) from None

    async def close(self, workspace_id: str) -> None:
        """Stop serving the workspace ``workspace_id`` and let go of its lock, leaving its flag
        as it is."""
        self._locks.pop(workspace_id).close()

    def _let_go(self) -> None:
        """Let go of the locks of the workspaces still served."""
        for lock in self._locks.values():
            lock.close()
        self._locks.clear()


def _let_go_prepared(preparing: asyncio.Future[_Prepared]) -> None:
    """Let go of the lock of the workspace ``preparing`` prepared for an add that was cancelled
    meanwhile, if it was prepared."""
    if preparing.cancelled() or preparing.exception() is not None:
        return
    served, _ = preparing.result()
    served.lock.close()


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


def workspace_config(root: Path, *, worktree: bool) -> OrbitConfig:
    """The configuration of the workspace at ``root``. Raises :class:`WorkspaceUnavailable`
    if it's invalid, or, with a ``worktree``, git has no identity for commits there."""
    problem = _identity_problem(root) if worktree else None
    if problem is not None:
        raise WorkspaceUnavailable(problem)
    try:
        return read_config(str(root))
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
    ``INTERNAL`` if it can't be made."""
    lock = WorkspaceLock(orbit_dir(str(root)))
    if url is not None:
        lock.announce(url)
    try:
        return lock, lock.take()
    except OSError as exc:
        raise WorkspaceUnavailable(
            f"Couldn't lock the repository with {lock.path}: {exc}", INTERNAL
        ) from None


async def _view(store: SqliteStore, root: Path, *, owned: bool, flag: bool) -> OpenedStore:
    """The workspace at ``root``'s view of ``store``, as :func:`open_workspace_store` opens it,
    recovering its fallback files if this orbit ``owned`` it, flagged as served if ``flag``. Raises
    :class:`WorkspaceUnavailable` as ``INTERNAL`` if that fails, as it may have changed the
    store or the workspace's files by then."""
    try:
        opened = await open_workspace_store(store, root, owned=owned)
        if flag:
            await _flagged(WorkspaceRegistry(store), opened.store.workspace_id)
    except (StoreError, ImportBlocked, RecoveryFailed) as exc:
        raise WorkspaceUnavailable(str(exc), INTERNAL) from None
    return opened


async def _flagged(registry: WorkspaceRegistry, workspace_id: str) -> None:
    """Flag the workspace ``workspace_id`` served in ``registry``; cancelled meanwhile, flag it
    not served again, so a server starting later doesn't serve a workspace whose add was
    cancelled."""
    try:
        await registry.set_served(workspace_id, True)
    except asyncio.CancelledError:
        # The store's one thread runs writes in order, so this lands after the flag, if it does.
        await registry.set_served(workspace_id, False)
        raise
