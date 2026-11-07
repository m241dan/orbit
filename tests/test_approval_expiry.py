"""When a request held for approval expires: an hour after the later of its hold and the last
frontend leaving, and never while a frontend is connected."""

from orbit.agent.domain.approvals import EXPIRY, expires_at


def test_a_held_request_expires_an_hour_after_its_hold_with_no_frontend_since() -> None:
    assert expires_at(100, 50) == 100 + EXPIRY


def test_it_expires_an_hour_after_the_last_frontend_left() -> None:
    assert expires_at(100, 500) == 500 + EXPIRY


def test_it_doesnt_expire_while_a_frontend_is_connected() -> None:
    assert expires_at(100, None) is None
