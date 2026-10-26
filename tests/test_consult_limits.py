"""The hourly limit on a role's consultations: counted over a rolling hour, lifted by 0, waived
for an hour by the user, and a held one expiring after an hour with no frontend connected."""

from orbit.agent.domain.consult_limits import HOUR, ConsultCount, expires_at


def asked_at(*times: float) -> ConsultCount:
    count = ConsultCount()
    for time in times:
        count = count.counted(time)
    return count


def test_consultations_in_the_past_hour_count() -> None:
    assert asked_at(0, 10, 20).recent(30) == 3


def test_consultations_older_than_an_hour_age_out() -> None:
    count = asked_at(0, 10, 20)
    assert count.recent(HOUR + 10) == 1


def test_counting_drops_the_consultations_aged_out() -> None:
    assert asked_at(0, HOUR + 1).asked == (HOUR + 1,)


def test_a_consultation_past_the_limit_needs_approval() -> None:
    assert asked_at(0, 10).needs_approval(2, 20)


def test_a_consultation_under_the_limit_needs_none() -> None:
    assert not asked_at(0).needs_approval(2, 20)


def test_one_needs_none_once_the_earlier_ones_age_out() -> None:
    assert not asked_at(0, 10).needs_approval(2, HOUR + 5)


def test_a_limit_of_0_means_no_limit() -> None:
    assert not asked_at(*range(100)).needs_approval(0, 100)


def test_a_role_freed_consults_without_approval_for_an_hour() -> None:
    count = asked_at(0, 10).freed(20)
    assert not count.needs_approval(2, 20 + HOUR - 1)


def test_being_freed_lapses_after_an_hour() -> None:
    count = asked_at(0, 10).freed(20).counted(HOUR).counted(HOUR + 10)
    assert count.needs_approval(2, HOUR + 20)


def test_a_held_consultation_expires_an_hour_after_its_hold_with_no_frontend_since() -> None:
    assert expires_at(100, 50) == 100 + HOUR


def test_it_expires_an_hour_after_the_last_frontend_left() -> None:
    assert expires_at(100, 500) == 500 + HOUR


def test_it_doesnt_expire_while_a_frontend_is_connected() -> None:
    assert expires_at(100, None) is None
