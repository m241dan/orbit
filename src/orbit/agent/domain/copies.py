"""When the copies conversational roles work in are removed: once their role's copy TTL has
passed since their conversation's last turn, and, left over, once no session records them."""

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta

from orbit.agent.domain.roles import DEFAULT_COPY_TTL_HOURS
from orbit.agent.domain.sessions import SavedCopy


def expired(copy: SavedCopy, ttl_hours: float, now: datetime) -> bool:
    """Whether ``copy`` is due for removal at ``now``: more than ``ttl_hours`` have passed since
    its conversation's last turn."""
    return now - copy.last_turn > timedelta(hours=ttl_hours)


def expired_roles(
    copies: Mapping[str, SavedCopy], ttls: Mapping[str, float], now: datetime
) -> list[str]:
    """The roles among ``copies`` whose copy is due for removal at ``now``, each by its TTL in
    ``ttls``, in hours; a role without one, such as one no longer configured, by the default."""
    return [
        role
        for role, copy in copies.items()
        if expired(copy, ttls.get(role, DEFAULT_COPY_TTL_HOURS), now)
    ]


def unrecorded(present: Iterable[str], recorded: Collection[Mapping[str, SavedCopy]]) -> list[str]:
    """The copies ``present``, by path, that none of the sessions' ``recorded`` copies is."""
    paths = {copy.path for copies in recorded for copy in copies.values()}
    return [path for path in present if path not in paths]
