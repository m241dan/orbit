"""Recovering, at startup, the events an earlier run parked in fallback files while the store
couldn't take them."""

from dataclasses import dataclass

from orbit.agent.application.ports import EventLog, ParkedEvents, ParkedFile


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


async def recover_parked(parked: ParkedEvents, log: EventLog) -> list[Recovered]:
    """Import every fallback file ``parked`` finds into ``log``, each file's events by ``seq``,
    skipping those stored already, then delete it; returns how many of each's were added. Raises
    :class:`RecoveryFailed` for the first that can't be read, imported or deleted, leaving it."""
    return [await _recover(parked, log, file) for file in await parked.parked()]


async def _recover(parked: ParkedEvents, log: EventLog, file: ParkedFile) -> Recovered:
    """Import ``file`` into ``log``, then delete it; returns how many of its events were added."""
    try:
        envelopes = sorted(await parked.read(file), key=lambda envelope: envelope.seq)
        added = await log.append(file.session_id, envelopes) if envelopes else 0
        await parked.delete(file)
    except RuntimeError as error:
        raise RecoveryFailed(file.path, str(error)) from error
    return Recovered(file.path, added)
