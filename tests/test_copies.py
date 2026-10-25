"""When a conversational role's copy is removed: once its role's TTL has passed since its
conversation's last turn, and, left over, once no session records it."""

from datetime import UTC, datetime, timedelta

from orbit.agent.domain.copies import expired, expired_roles, unrecorded
from orbit.agent.domain.sessions import SavedCopy

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def copy(path: str, hours_ago: float) -> SavedCopy:
    return SavedCopy(path=path, last_turn=NOW - timedelta(hours=hours_ago))


def test_a_copy_whose_ttl_has_passed_since_its_last_turn_is_expired() -> None:
    assert expired(copy("/c", 3), 2, NOW)


def test_a_copy_within_its_ttl_is_kept() -> None:
    assert not expired(copy("/c", 2), 2, NOW)


def test_each_role_is_held_to_its_own_ttl() -> None:
    copies = {"experimenter": copy("/a", 3), "architect": copy("/b", 3)}
    assert expired_roles(copies, {"experimenter": 2, "architect": 4}, NOW) == ["experimenter"]


def test_a_role_without_a_ttl_is_held_to_the_default() -> None:
    copies = {"gone": copy("/a", 25), "young": copy("/b", 23)}
    assert expired_roles(copies, {}, NOW) == ["gone"]


def test_copies_no_session_records_are_unrecorded() -> None:
    recorded = [{"experimenter": copy("/a", 1)}, {"architect": copy("/b", 1)}]
    assert unrecorded(["/a", "/b", "/c"], recorded) == ["/c"]
