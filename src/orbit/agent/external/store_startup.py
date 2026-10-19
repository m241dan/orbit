"""Opening a workspace's store at startup: importing the sessions an older orbit saved as files,
then recovering the events an earlier run parked in fallback files."""

from dataclasses import dataclass
from pathlib import Path

from orbit.agent.application.recovery import recover_parked
from orbit.agent.domain.storage_halt import recovered_note
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.repository import orbit_dir
from orbit.agent.external.session_import import import_session_files, imported_note
from orbit.agent.external.sqlite_store import SqliteStore


@dataclass(frozen=True)
class OpenedStore:
    """The store, the notes on what was imported into it, and the workspace's fallback files."""

    store: SqliteStore
    notes: list[str]
    fallback: FallbackFiles


async def open_workspace_store(state_dir: Path, root: Path) -> OpenedStore:
    """The store in ``state_dir`` for the workspace at ``root``, with the sessions an older orbit
    saved as files and the events an earlier run parked in fallback files imported, the notes on
    what was imported, and the fallback files.

    Raises :class:`StoreError` if the store can't be opened, :class:`ImportBlocked` if the old
    sessions can't be imported and :class:`RecoveryFailed` if a fallback file can't be
    recovered; the store is closed if an import fails.
    """
    store = await SqliteStore.open(state_dir, str(root))
    try:
        fallback = FallbackFiles(state_dir, store.workspace_id, orbit_dir(str(root)))
        return OpenedStore(store, await _imported(store, fallback, root), fallback)
    except BaseException:
        await store.close()
        raise


async def _imported(store: SqliteStore, fallback: FallbackFiles, root: Path) -> list[str]:
    """Import into ``store`` the old session files of the workspace at ``root``, then its
    ``fallback`` files; returns the notes on what was imported."""
    legacy = orbit_dir(str(root))
    imported = await import_session_files(store, legacy)
    notes = [] if imported is None else [imported_note(imported, _shown(legacy, root))]
    recovered = await recover_parked(fallback, store)
    return notes + [
        recovered_note(file.count, str(_shown(Path(file.path), root)))
        for file in recovered
        if file.count
    ]


def _shown(path: Path, root: Path) -> Path:
    """``path`` as shown to the user: relative to the workspace at ``root``, if it's inside."""
    return path.relative_to(root) if path.is_relative_to(root) else path
