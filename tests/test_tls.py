"""Whether ``orbit serve`` can serve as its TLS settings say, checked before it starts."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.agent.domain.server import (
    CertificateDates,
    LocalTls,
    ServerSettings,
    certificate_dates,
    missing_tls_key,
)
from orbit.agent.external.tls import LocalDiscoveryUnavailable, TlsError, local_tls, tls_warnings
from tests.tls_certs import DAY, ServerFiles, make_ca, make_server, other_key

NOW = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture
def files(tmp_path: Path) -> ServerFiles:
    return make_server(make_ca(tmp_path), tmp_path)


def _tls(files: ServerFiles, **changes: str) -> ServerSettings:
    settings = {"tls": "tls", "cert": str(files.cert), "key": str(files.key)} | changes
    return ServerSettings.model_validate(settings)


def test_plaintext_on_loopback_is_served_without_warnings() -> None:
    assert tls_warnings(ServerSettings()) == ()


def test_plaintext_beyond_loopback_is_refused_pointing_at_tls() -> None:
    with pytest.raises(TlsError, match=r"^Plaintext is for loopback alone\..*0\.0\.0\.0.*--tls"):
        tls_warnings(ServerSettings(host="0.0.0.0"))


def test_a_valid_certificate_and_its_key_are_served_beyond_loopback(files: ServerFiles) -> None:
    assert tls_warnings(_tls(files, host="0.0.0.0")) == ()


@pytest.mark.parametrize(
    ("settings", "key", "flag"),
    [
        (ServerSettings(tls="tls", cert="c.pem"), "key", "--key"),
        (ServerSettings(tls="tls", key="k.pem"), "cert", "--cert"),
        (ServerSettings(tls="mtls", cert="c.pem", key="k.pem"), "client_ca", "--client-ca"),
    ],
)
def test_a_mode_missing_a_file_is_refused_naming_its_key(
    settings: ServerSettings, key: str, flag: str
) -> None:
    with pytest.raises(TlsError, match=f"^The TLS mode is missing a file.* needs {key}: .*{flag}"):
        tls_warnings(settings)


def test_a_key_others_can_read_is_refused_with_the_chmod_that_fixes_it(
    files: ServerFiles,
) -> None:
    files.key.chmod(0o644)
    with pytest.raises(TlsError, match=re.escape(f"run chmod 600 {files.key}.")):
        tls_warnings(_tls(files))


def test_a_missing_key_is_refused_naming_it(files: ServerFiles, tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    with pytest.raises(TlsError, match=f"^A TLS file can't be read. {re.escape(str(missing))}: "):
        tls_warnings(_tls(files, key=str(missing)))


@pytest.mark.parametrize("which", ["cert", "key"])
def test_a_file_that_isnt_pem_is_refused_naming_it(
    files: ServerFiles, tmp_path: Path, which: str
) -> None:
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not PEM\n")
    garbage.chmod(0o600)
    with pytest.raises(TlsError, match=f"{re.escape(str(garbage))}: not a"):
        tls_warnings(_tls(files, **{which: str(garbage)}))


def test_a_missing_certificate_is_refused_naming_it(files: ServerFiles, tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    with pytest.raises(TlsError, match=f"{re.escape(str(missing))}: No such file"):
        tls_warnings(_tls(files, cert=str(missing)))


def test_a_key_that_isnt_the_certificates_is_refused(files: ServerFiles, tmp_path: Path) -> None:
    key = other_key(tmp_path)
    with pytest.raises(TlsError, match=r"^The certificate and key don't match"):
        tls_warnings(_tls(files, key=str(key)))


def test_an_expired_certificate_is_refused_naming_its_dates(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path, valid_from=-10 * DAY, valid_for=5 * DAY)
    with pytest.raises(TlsError, match=r"^The server certificate has expired\..* valid from 20"):
        tls_warnings(_tls(files))


def test_a_certificate_not_valid_yet_is_refused_naming_its_dates(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path, valid_from=5 * DAY)
    with pytest.raises(TlsError, match=r"^The server certificate isn't valid yet\..* from 20"):
        tls_warnings(_tls(files))


def test_a_certificate_expiring_within_two_weeks_is_served_with_a_warning(tmp_path: Path) -> None:
    files = make_server(make_ca(tmp_path), tmp_path, valid_for=10 * DAY)
    [warning] = tls_warnings(_tls(files))
    assert warning.startswith("The server certificate expires soon.")
    assert "renew it and restart orbit serve" in warning


@pytest.mark.parametrize(
    ("start", "end", "dates"),
    [
        (NOW + DAY, NOW + 30 * DAY, CertificateDates.NOT_YET_VALID),
        (NOW - 30 * DAY, NOW - DAY, CertificateDates.EXPIRED),
        (NOW - DAY, NOW + 13 * DAY, CertificateDates.EXPIRING),
        (NOW - DAY, NOW + 14 * DAY, CertificateDates.VALID),
    ],
)
def test_where_now_falls_in_a_certificates_validity(
    start: datetime, end: datetime, dates: CertificateDates
) -> None:
    assert certificate_dates(start, end, NOW) == dates


def test_plaintext_needs_no_files() -> None:
    assert missing_tls_key(ServerSettings()) is None
    assert missing_tls_key(ServerSettings(tls="tls", cert="c", key="k")) is None


def test_a_local_client_trusts_the_ca_and_connects_by_a_name_the_certificate_gives(
    files: ServerFiles, tmp_path: Path
) -> None:
    settings = _tls(files, ca="ca.pem", client_ca="client-ca.pem", host="0.0.0.0")
    assert local_tls(settings, tmp_path) == LocalTls(
        mutual=False, ca=str(tmp_path / "ca.pem"), hostname="localhost"
    )


def test_under_mtls_a_local_client_shows_a_certificate_and_trusts_the_client_ca_without_a_ca(
    files: ServerFiles, tmp_path: Path
) -> None:
    settings = _tls(files, tls="mtls", client_ca=str(tmp_path / "ca.pem"))
    assert local_tls(settings, tmp_path) == LocalTls(
        mutual=True, ca=str(tmp_path / "ca.pem"), hostname="127.0.0.1"
    )


def test_without_a_ca_local_discovery_is_unavailable_naming_the_setting(
    files: ServerFiles, tmp_path: Path
) -> None:
    with pytest.raises(
        LocalDiscoveryUnavailable,
        match=r"^Local discovery won't work without a CA\..*set ca in \[server\]",
    ):
        local_tls(_tls(files), tmp_path)


def test_a_certificate_naming_no_local_name_leaves_local_discovery_unavailable(
    tmp_path: Path,
) -> None:
    files = make_server(make_ca(tmp_path), tmp_path, hosts=("orbit.example.lan",))
    settings = _tls(files, ca="ca.pem", host="0.0.0.0")
    message = r"^Local discovery won't work with this certificate\..*localhost, 127\.0\.0\.1, ::1"
    with pytest.raises(LocalDiscoveryUnavailable, match=message):
        local_tls(settings, tmp_path)


def test_a_certificate_without_alternative_names_leaves_local_discovery_unavailable(
    tmp_path: Path,
) -> None:
    files = make_server(make_ca(tmp_path), tmp_path, hosts=())
    with pytest.raises(LocalDiscoveryUnavailable, match="with this certificate"):
        local_tls(_tls(files, ca="ca.pem"), tmp_path)
