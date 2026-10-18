"""Reading back a session's earlier events, a page at a time."""

from orbit.agent.application.keeper import SessionKeeper
from orbit.agent.application.outbox import Outbox
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.sessions import missing_message
from orbit.protocol import UNKNOWN, History, HistoryPage


class SessionHistory:
    """Answers ``History`` from the log ``events`` keeps, for the session open in ``keeper``
    and those saved beside it."""

    def __init__(self, keeper: SessionKeeper, events: Outbox) -> None:
        self._keeper = keeper
        self._events = events

    async def page(self, command: History) -> HistoryPage:
        """The page of events ``command`` asks for; refused for a session that isn't this
        workspace's."""
        session_id = command.session
        if not await self._known(session_id):
            raise Refused(UNKNOWN, missing_message(session_id))
        events, more = await self._events.history(session_id, command.before_seq, command.limit)
        return HistoryPage(session=session_id, events=events, more=more)

    async def _known(self, session_id: str) -> bool:
        """Whether the session ``session_id`` is the one open or a saved one."""
        if session_id == self._keeper.id:
            return True
        return any(summary.id == session_id for summary in await self._keeper.summaries())
