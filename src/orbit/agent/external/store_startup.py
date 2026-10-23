"""Opening a workspace's view of the store at startup: importing the sessions an older orbit
saved as files, then, if this orbit owns the workspace, recovering the events an earlier run
parked in fallback files."""

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

from orbit.agent.application.recovery import recover_parked
from orbit.agent.domain.sessions import imported_note
from orbit.agent.domain.storage_halt import recovered_note
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.repository import orbit_dir
from orbit.agent.external.session_files import SESSIONS
from orbit.agent.external.session_import import import_session_files
from orbit.agent.external.sqlite_store import SqliteStore, WorkspaceStore
from orbit.agent.external.workspace_registry import WorkspaceRegistry


@dataclass(frozen=True)
class OpenedStore:
    """The workspace at ``root``'s view of the store, the notes on what was imported into it,
    the workspace's fallback files, and whether it's ``managed``, a clone orbit made."""

    store: WorkspaceStore
    notes: list[str]
    fallback: FallbackFiles
    root: Path
    managed: bool = False

    async def recover(self) -> list[str]:
        """Import the workspace's fallback files into its view of the store, then delete them;
        returns the notes on what was recovered. Raises :class:`RecoveryFailed` if a fallback
        file can't be recovered."""
        return await _recovered(self.store, self.fallback, self.root)


async def open_workspace_store(store: SqliteStore, root: Path, *, owned: bool) -> OpenedStore:
    """The view of ``store`` for the workspace at ``root``, registered if it isn't yet (or taken
    over from where its repository was before it moved, which is noted first), with the
    sessions an older orbit saved as files imported and, if this orbit ``owned`` the workspace,
    the events an earlier run parked in fallback files too, the notes on what was imported, and
    the fallback files. An orbit that doesn't own it leaves the fallback files to the owner,
    which may still be writing them.

    Raises :class:`StoreError` if the workspace can't be registered,
    :class:`RepositoryUnreadable` if git can't find orbit's directory in its repository,
    :class:`ImportBlocked` if the old sessions can't be imported and :class:`RecoveryFailed` if a
    fallback file can't be recovered.
    """
    registered = await WorkspaceRegistry(store).find_or_register(str(root))
    view = store.workspace_view(registered.workspace_id)
    orbit = await asyncio.to_thread(orbit_dir, str(root))
    fallback = FallbackFiles(store.state_dir, registered.workspace_id, orbit)
    moved = [] if registered.note is None else [registered.note]
    opened = OpenedStore(view, moved + await _imported(view, orbit, root), fallback, root)
    if not owned:
        return opened
    return replace(opened, notes=opened.notes + await opened.recover())


async def _imported(store: WorkspaceStore, legacy: Path, root: Path) -> list[str]:
    """Import into ``store`` the old session files in ``legacy``, orbit's directory for the
    workspace at ``root``; returns the notes on what was imported."""
    imported = await import_session_files(store, legacy)
    if imported is None:
        return []
    sessions = str(_shown(legacy, root) / SESSIONS)
    return [imported_note(imported.count, sessions, imported.folder)]


async def _recovered(store: WorkspaceStore, fallback: FallbackFiles, root: Path) -> list[str]:
    """Import into ``store`` the ``fallback`` files of the workspace at ``root``, then delete
    them; returns the notes on what was recovered."""
    recovered = await recover_parked(fallback, store)
    return [
        recovered_note(file.count, str(_shown(Path(file.path), root)))
        for file in recovered
        if file.count
    ]


def _shown(path: Path, root: Path) -> Path:
    """``path`` as shown to the user: relative to the workspace at ``root``, if it's inside."""
    return path.relative_to(root) if path.is_relative_to(root) else path
