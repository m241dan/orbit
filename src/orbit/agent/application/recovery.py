"""Recovering, at startup, the events an earlier run parked in fallback files while the store
couldn't take them."""

from dataclasses import dataclass

from orbit.agent.application.ports import ParkedEvents, ParkedFile, SessionStore, StoreFailed


class RecoveryFailed(Exception):
    """The fallback file at ``path`` couldn't be read, imported or deleted, as ``reason`` says."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"Couldn't recover the events parked in {path}: {reason}")


@dataclass(frozen=True)
class Recovered:
    """A fallback file imported into the log: its ``path``, and the ``count`` of its events that
    weren't stored already."""

    path: str
    count: int


async def recover_parked(parked: ParkedEvents, log: SessionStore) -> list[Recovered]:
    """Import every fallback file ``parked`` finds into ``log``, each file's events by ``seq``,
    skipping those stored already, then delete it; returns how many of each's were added. A
    legacy file is left alone unless ``log`` has its session, as another workspace's may be.
    Raises :class:`RecoveryFailed` for the first that can't be read, imported or deleted, leaving
    it, or when the sessions can't be listed to tell a legacy file's."""
    files = await parked.parked()
    owned = await _owned(log, [file for file in files if file.legacy])
    return [
        await _recover(parked, log, file)
        for file in files
        if not file.legacy or file.session_id in owned
    ]


async def _owned(log: SessionStore, legacy: list[ParkedFile]) -> set[str]:
    """The ids of the sessions ``log`` has, when there are ``legacy`` files to tell; raises
    :class:`RecoveryFailed`, naming the first, when they can't be listed."""
    if not legacy:
        return set()
    try:
        return {session.id for session in await log.sessions()}
    except StoreFailed as error:
        raise RecoveryFailed(legacy[0].path, str(error)) from error


async def _recover(parked: ParkedEvents, log: SessionStore, file: ParkedFile) -> Recovered:
    """Import ``file`` into ``log``, then delete it; returns how many of its events were added."""
    try:
        envelopes = sorted(await parked.read(file), key=lambda envelope: envelope.seq)
        added = await log.append(file.session_id, envelopes) if envelopes else 0
        await parked.delete(file)
    except StoreFailed as error:
        raise RecoveryFailed(file.path, str(error)) from error
    return Recovered(file.path, added)
