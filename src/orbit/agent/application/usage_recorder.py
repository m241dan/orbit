"""Recording what each finished turn cost, in the background, so publishing never waits on it."""

import asyncio

from orbit.agent.application.failures import outcome
from orbit.agent.application.keeper import Clock
from orbit.agent.application.lanes import Background
from orbit.agent.application.ports import AgentLaunch, Publish, StoreFailed, UsageLedger
from orbit.agent.application.storage_halt import Halting
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.usage_totals import UsageRow, counted
from orbit.protocol import Usage


class UsageRecorder:
    """Records each finished turn's usage in ``ledger``, if there is one, as a row timed by
    ``clock``, writing in the background; a failure in the background is reported with
    ``publish``. A row that can't be written halts the session through ``halt`` and is kept, with
    those after it, to be written when the halt is retried; nothing is written while it's
    halted."""

    def __init__(
        self, ledger: UsageLedger | None, halt: Halting, publish: Publish, clock: Clock
    ) -> None:
        self._ledger = ledger
        self._halt = halt
        self._clock = clock
        self._pending: list[UsageRow] = []  # The rows not yet written, oldest first.
        self._writer = Background("usage", publish)
        self._writing = asyncio.Lock()  # Writes the rows once at a time, a retry's included.

    def record(self, workspace_id: str, session_id: str, launch: AgentLaunch, usage: Usage) -> None:
        """Record ``usage``, a finished turn's, in the session ``session_id`` of the workspace
        ``workspace_id``, by the agent ``launch`` started, in its role and on its model."""
        if self._ledger is None:
            return
        model = launch.settings.model
        row = UsageRow(
            workspace_id, session_id, launch.agent.role, model, counted(usage), self._clock()
        )
        self._pending.append(row)
        # A write under way carries on to the rows added meanwhile.
        self._writer.start(self._write_pending(self._ledger))

    async def _write_pending(self, ledger: UsageLedger) -> None:
        """Write the rows not yet written, unless the session is halted, halting it if one
        fails."""
        if self._halt.halted:
            return
        error = await self._write(ledger)
        if error is not None:
            self._halt.fail(FailedWrite.USAGE, error)

    async def retry(self) -> StoreFailed | None:
        """Write the rows not yet written, though the session is halted, as resuming does;
        returns the error when one fails."""
        if self._ledger is None:
            return None
        return await self._write(self._ledger)

    async def _write(self, ledger: UsageLedger) -> StoreFailed | None:
        """Write the rows not yet written, oldest first, until one fails; returns its error."""
        async with self._writing:
            while self._pending:
                error = await outcome(ledger.record(self._pending[0]))
                if isinstance(error, StoreFailed):
                    return error
                self._pending.pop(0)
        return None

    async def close(self) -> None:
        """Stop writing; the rows not yet written are left unwritten, their cost still counted
        in the session's spend."""
        await self._writer.close()
