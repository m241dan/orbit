"""What ``orbit certs list`` shows of each certificate in ``certs``: its kind, its name and a
server's names, when it expires, the days left and its fingerprint, marked when it has expired,
expires soon or is revoked."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from orbit.agent.domain.certificate_list import (
    CertificateKind,
    ListedCertificate,
    certificate_lines,
)

NOW = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)
DAY = timedelta(days=1)
CA_PRINT = ":".join(["AA"] * 32)
SERVER_PRINT = ":".join(["BB"] * 32)
LAPTOP_PRINT = ":".join(["CC"] * 32)


def _certificate(
    kind: CertificateKind,
    name: str,
    fingerprint: str,
    days: int = 365,
    names: tuple[str, ...] = (),
) -> ListedCertificate:
    return ListedCertificate(
        kind=kind,
        name=name,
        not_before=NOW - DAY,
        not_after=NOW + days * DAY,
        fingerprint=fingerprint,
        names=names,
    )


CA = _certificate("CA", "orbit CA (box)", CA_PRINT, days=3650)
SERVER = _certificate("server", "box", SERVER_PRINT, names=("box", "localhost", "127.0.0.1"))
LAPTOP = _certificate("client", "laptop", LAPTOP_PRINT)


def test_a_certificate_has_a_line_of_its_kind_name_expiry_days_left_and_fingerprint() -> None:
    assert certificate_lines([LAPTOP], (), NOW) == (
        f"client  laptop  2027-10-25  365 days left  {LAPTOP_PRINT}",
    )


def test_the_lines_are_in_columns_two_spaces_apart() -> None:
    assert certificate_lines([CA, SERVER, LAPTOP], (), NOW) == (
        f"CA      orbit CA (box)                   2036-10-22  3650 days left  {CA_PRINT}",
        f"server  box (box, localhost, 127.0.0.1)  2027-10-25  365 days left   {SERVER_PRINT}",
        f"client  laptop                           2027-10-25  365 days left   {LAPTOP_PRINT}",
    )


@pytest.mark.parametrize(
    ("certificate", "name"),
    [
        (replace(LAPTOP, names=("laptop.lan",)), "laptop"),
        (replace(CA, names=("box",)), "orbit CA (box)"),
        (replace(SERVER, names=()), "box"),
    ],
)
def test_only_a_server_certificate_shows_the_names_it_holds(
    certificate: ListedCertificate, name: str
) -> None:
    [line] = certificate_lines([certificate], (), NOW)

    assert line.split("  ")[1] == name


@pytest.mark.parametrize(
    ("days", "status"),
    [
        (10, "10 days left, expires soon"),
        (1, "1 day left, expires soon"),
        (14, "14 days left"),
    ],
)
def test_a_certificate_expiring_within_14_days_is_marked_expires_soon(
    days: int, status: str
) -> None:
    certificate = replace(LAPTOP, not_after=NOW + days * DAY)

    assert certificate_lines([certificate], (), NOW) == (
        f"client  laptop  {NOW + days * DAY:%Y-%m-%d}  {status}  {LAPTOP_PRINT}",
    )


def test_an_expired_certificate_is_marked_expired_with_no_days_left() -> None:
    certificate = replace(LAPTOP, not_before=NOW - 100 * DAY, not_after=NOW - 5 * DAY)

    assert certificate_lines([certificate], (), NOW) == (
        f"client  laptop  2026-10-20  expired  {LAPTOP_PRINT}",
    )


def test_a_certificate_not_valid_yet_is_marked_so() -> None:
    certificate = replace(LAPTOP, not_before=NOW + DAY)

    assert certificate_lines([certificate], (), NOW) == (
        f"client  laptop  2027-10-25  365 days left, not yet valid  {LAPTOP_PRINT}",
    )


@pytest.mark.parametrize("entry", [LAPTOP_PRINT, LAPTOP_PRINT.replace(":", "").lower()])
def test_a_client_certificate_whose_fingerprint_is_revoked_is_marked_revoked(entry: str) -> None:
    assert certificate_lines([LAPTOP], (entry,), NOW) == (
        f"client  laptop  2027-10-25  365 days left, revoked  {LAPTOP_PRINT}",
    )


def test_an_expired_revoked_client_certificate_is_marked_both() -> None:
    certificate = replace(LAPTOP, not_before=NOW - 100 * DAY, not_after=NOW - 5 * DAY)

    [line] = certificate_lines([certificate], (LAPTOP_PRINT,), NOW)

    assert line.split("  ")[3] == "expired, revoked"


def test_only_client_certificates_are_marked_revoked() -> None:
    lines = certificate_lines([CA, SERVER], (CA_PRINT, SERVER_PRINT), NOW)

    assert not any("revoked" in line for line in lines)


def test_days_left_are_counted_in_calendar_days_in_utc() -> None:
    late = datetime(2026, 10, 26, 1, 0, tzinfo=timezone(timedelta(hours=2)))  # 23:00 UTC.
    certificate = replace(LAPTOP, not_after=datetime(2026, 10, 27, 1, 0, tzinfo=UTC))

    assert certificate_lines([certificate], (), late) == (
        f"client  laptop  2026-10-27  2 days left, expires soon  {LAPTOP_PRINT}",
    )


def test_no_certificates_have_no_lines() -> None:
    assert certificate_lines([], (), NOW) == ()
