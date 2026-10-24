"""Paging back through the open session's history, a page above the conversation at a time."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from orbit.protocol import Event, History, HistoryPage, Snapshot
from orbit.tui.view.outcomes import Awaited, Send

LOADING_EARLIER = "Loading earlier messages…"
"""Shown at the top of the conversation while a page of earlier events loads."""

TRIMMED_EARLIER = "Older events were removed (session_log_mb)"
"""Shown at the top of the conversation once paging reaches events trimmed from the log."""


@dataclass(frozen=True)
class Paging:
    """How far back the conversation shows the session ``session``: from the event ``oldest``,
    by its ``seq``, if any; whether ``more`` may come before it; whether a page of them is
    ``loading``; and whether events before the oldest were ``trimmed`` from the log."""

    session: str | None = None
    oldest: int | None = None
    more: bool = False
    loading: bool = False
    trimmed: bool = False


def paging_from(snapshot: Snapshot) -> Paging:
    """How far back ``snapshot``'s transcript goes; with any, earlier events may remain."""
    oldest = snapshot.oldest_seq
    return Paging(session=snapshot.session.id, oldest=oldest, more=oldest is not None)


def earlier_request(paging: Paging) -> History | None:
    """The ``History`` asking for the page before the oldest event shown; none while one loads
    or when no earlier events remain."""
    if paging.loading or not paging.more or paging.oldest is None or paging.session is None:
        return None
    return History(session=paging.session, before_seq=paging.oldest)


def loading(paging: Paging, waiting: bool) -> Paging:
    """``paging``, ``waiting`` for a page or no longer."""
    return replace(paging, loading=waiting)


def page_arrived(paging: Paging, page: HistoryPage) -> Paging | None:
    """How far back the conversation goes with ``page`` shown above it; None when ``page``
    isn't one ``paging`` waits for. A page empty as sent ends the paging, as it moves nothing
    back, and the last page says whether earlier events were trimmed."""
    if not paging.loading or page.session != paging.session:
        return None
    oldest = page.oldest_seq if page.oldest_seq is not None else paging.oldest
    more = page.more and page.oldest_seq is not None
    trimmed = page.trimmed and not page.more
    return Paging(session=paging.session, oldest=oldest, more=more, trimmed=trimmed)


class PagingView(Protocol):
    """Where the paging shows, above the conversation's entries."""

    def show_loading(self, loading: bool) -> None:
        """Show that earlier entries are ``loading``, or stop."""
        ...

    def show_trimmed(self, trimmed: bool) -> None:
        """Show that earlier entries were ``trimmed`` from the log, or stop."""
        ...


class Feed(Protocol):
    """What shows the conversation's events."""

    def prepend(self, events: Iterable[Event]) -> None:
        """Show ``events``, which came before every event shown, above them."""
        ...


class HistoryPager:
    """Asks with ``send`` for the page of events before the oldest the conversation shows,
    one page at a time, showing in ``view`` that it loads until it arrives or is refused,
    and has ``feed`` show the page above the conversation once it arrives; ``view`` shows
    too when the last page reaches events trimmed from the log."""

    def __init__(self, view: PagingView, feed: Feed, send: Send) -> None:
        self._view = view
        self._feed = feed
        self._send = send
        self._paging = Paging()

    def follow(self, snapshot: Snapshot) -> None:
        """Page back from ``snapshot``'s transcript, which replaced the conversation,
        forgetting any page still loading."""
        self._paging = paging_from(snapshot)
        self._view.show_loading(False)
        self._view.show_trimmed(False)

    async def earlier(self) -> None:
        """Ask for the page before the oldest event shown, unless one loads or none remain."""
        request = earlier_request(self._paging)
        if request is None:
            return
        self._show_loading(True)
        # A request sent after a snapshot reset the paging supersedes any still loading.
        await self._send(request, Awaited(refused=self._refused, supersedes=True))

    def _refused(self) -> None:
        """Stop waiting for the page asked for, which was refused or not sent, so it can be
        asked for again."""
        self._show_loading(False)

    def show(self, page: HistoryPage) -> None:
        """Show ``page`` above the conversation, if it's the page asked for."""
        paging = page_arrived(self._paging, page)
        if paging is None:
            return
        self._paging = paging
        self._view.show_loading(False)
        self._view.show_trimmed(paging.trimmed)
        self._feed.prepend(envelope.payload for envelope in page.events)

    def _show_loading(self, waiting: bool) -> None:
        """Note that a page is loading if ``waiting``, or no longer, and show it."""
        self._paging = loading(self._paging, waiting)
        self._view.show_loading(waiting)
