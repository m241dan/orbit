"""When a request held for approval expires: an hour after the later of its hold and the last
frontend leaving, and never while a frontend is connected."""

from datetime import timedelta

from orbit.agent.domain.approvals import expires_at


def test_a_held_request_expires_an_hour_after_its_hold_with_no_frontend_since() -> None:
    held = timedelta(seconds=100)
    assert expires_at(held, timedelta(seconds=50)) == held + timedelta(hours=1)


def test_it_expires_an_hour_after_the_last_frontend_left() -> None:
    alone_since = timedelta(seconds=500)
    assert expires_at(timedelta(seconds=100), alone_since) == alone_since + timedelta(hours=1)


def test_it_doesnt_expire_while_a_frontend_is_connected() -> None:
    assert expires_at(timedelta(seconds=100), None) is None
