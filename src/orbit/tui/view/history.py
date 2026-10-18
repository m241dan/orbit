"""Paging back through the open session's history, a page above the conversation at a time."""

from dataclasses import dataclass, replace

from orbit.protocol import History, HistoryPage, Snapshot

LOADING_EARLIER = "Loading earlier messages…"
"""Shown at the top of the conversation while a page of earlier events loads."""


@dataclass(frozen=True)
class Paging:
    """How far back the conversation shows the session ``session``: from the event ``oldest``,
    by its ``seq``, if any; whether ``more`` may come before it; and whether a page of them is
    ``loading``."""

    session: str | None = None
    oldest: int | None = None
    more: bool = False
    loading: bool = False


def paging_from(snapshot: Snapshot) -> Paging:
    """How far back ``snapshot``'s transcript goes; with any, earlier events may remain."""
    transcript = snapshot.transcript
    oldest = transcript[0].seq if transcript else None
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
    isn't one ``paging`` waits for. An empty page ends the paging, as it moves nothing back."""
    if not paging.loading or page.session != paging.session:
        return None
    oldest = page.events[0].seq if page.events else paging.oldest
    return Paging(session=paging.session, oldest=oldest, more=page.more and bool(page.events))
