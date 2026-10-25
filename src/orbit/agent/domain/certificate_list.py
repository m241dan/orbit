"""What ``orbit certs list`` shows of the certificates in ``certs`` in the config directory:
each one's kind, its name and a server's names, when it expires and its fingerprint, and whether
it has expired, expires soon or is revoked."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from orbit.agent.domain.server import CertificateDates, certificate_dates, is_revoked

CertificateKind = Literal["CA", "server", "client"]
"""What a certificate in ``certs`` is: the CA, the server's or a client's."""


@dataclass(frozen=True)
class ListedCertificate:
    """A certificate in ``certs`` of the ``kind``, ``name``d by its file if a client's, else by
    its common name, holding ``names`` as subject alternative names, valid from ``not_before``
    until ``not_after``, with the SHA-256 ``fingerprint``."""

    kind: CertificateKind
    name: str
    not_before: datetime
    not_after: datetime
    fingerprint: str
    names: tuple[str, ...] = ()


def certificate_lines(
    certificates: Sequence[ListedCertificate], revoked: Collection[str], now: datetime
) -> tuple[str, ...]:
    """A line for each of ``certificates`` at ``now``, in columns: its kind, its name with a
    server's names, its expiry date, the days left or that it has expired, marked when it
    expires soon, isn't valid yet or, a client's, is among the ``revoked`` fingerprints, and its
    fingerprint."""
    rows = [_row(certificate, revoked, now) for certificate in certificates]
    widths = [max(map(len, column)) for column in zip(*rows, strict=True)]
    return tuple(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _row(
    certificate: ListedCertificate, revoked: Collection[str], now: datetime
) -> tuple[str, ...]:
    """The cells of ``certificate``'s line at ``now``, given the ``revoked`` fingerprints."""
    status = (*_validity(certificate, now), *_revocation(certificate, revoked))
    return (
        certificate.kind,
        _name(certificate),
        f"{_utc_date(certificate.not_after):%Y-%m-%d}",
        ", ".join(status),
        certificate.fingerprint,
    )


def _name(certificate: ListedCertificate) -> str:
    """``certificate``'s name, a server's with the names it holds."""
    if certificate.kind != "server" or not certificate.names:
        return certificate.name
    return f"{certificate.name} ({', '.join(certificate.names)})"


def _validity(certificate: ListedCertificate, now: datetime) -> tuple[str, ...]:
    """The days ``certificate`` has left at ``now``, or that it has expired, then whether it
    expires soon or isn't valid yet."""
    match certificate_dates(certificate.not_before, certificate.not_after, now):
        case CertificateDates.EXPIRED:
            return ("expired",)
        case CertificateDates.EXPIRING:
            return (_days_left(certificate.not_after, now), "expires soon")
        case CertificateDates.NOT_YET_VALID:
            return (_days_left(certificate.not_after, now), "not yet valid")
        case _:
            return (_days_left(certificate.not_after, now),)


def _days_left(not_after: datetime, now: datetime) -> str:
    """The days from ``now`` to ``not_after``, counted by their dates in UTC."""
    days = (_utc_date(not_after) - _utc_date(now)).days
    return "1 day left" if days == 1 else f"{days} days left"


def _utc_date(moment: datetime) -> date:
    """The date in UTC at ``moment``."""
    return moment.astimezone(UTC).date()


def _revocation(certificate: ListedCertificate, revoked: Collection[str]) -> tuple[str, ...]:
    """That ``certificate`` is revoked, if it's a client's whose fingerprint is among the
    ``revoked`` ones."""
    if certificate.kind == "client" and is_revoked(certificate.fingerprint, revoked):
        return ("revoked",)
    return ()
