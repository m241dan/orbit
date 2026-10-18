"""Paging the conversation back through the open session's history."""

from collections.abc import Awaitable, Callable

from orbit.protocol import Command, HistoryPage, Snapshot
from orbit.tui.conversation import ConversationFeed
from orbit.tui.transcript import Transcript
from orbit.tui.view.history import Paging, earlier_request, loading, page_arrived, paging_from


class HistoryPager:
    """Asks with ``send`` for the page of events before the oldest the conversation shows,
    one page at a time, showing in ``transcript`` that it loads, and has ``feed`` show the page
    above the conversation once it arrives; ``transcript`` shows too when the last page reaches
    events trimmed from the log."""

    def __init__(
        self,
        transcript: Transcript,
        feed: ConversationFeed,
        send: Callable[[Command], Awaitable[bool]],
    ) -> None:
        self._transcript = transcript
        self._feed = feed
        self._send = send
        self._paging = Paging()

    def follow(self, snapshot: Snapshot) -> None:
        """Page back from ``snapshot``'s transcript, which replaced the conversation,
        forgetting any page still loading."""
        self._paging = paging_from(snapshot)
        self._transcript.show_loading(False)
        self._transcript.show_trimmed(False)

    async def earlier(self) -> None:
        """Ask for the page before the oldest event shown, unless one loads or none remain."""
        request = earlier_request(self._paging)
        if request is None:
            return
        self._show_loading(True)
        if not await self._send(request):
            self._show_loading(False)

    def show(self, page: HistoryPage) -> None:
        """Show ``page`` above the conversation, if it's the page asked for."""
        paging = page_arrived(self._paging, page)
        if paging is None:
            return
        self._paging = paging
        self._transcript.show_loading(False)
        self._transcript.show_trimmed(paging.trimmed)
        self._feed.prepend(envelope.payload for envelope in page.events)

    def _show_loading(self, waiting: bool) -> None:
        """Note that a page is loading if ``waiting``, or no longer, and show it."""
        self._paging = loading(self._paging, waiting)
        self._transcript.show_loading(waiting)
