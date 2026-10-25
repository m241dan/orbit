"""``orbit certs list``: each certificate in ``certs`` in the config directory, with when it
expires, the days left and its fingerprint, marking the client certificates ``[server]
revoked`` lists."""

import re
import socket
from pathlib import Path

import pytest
from cryptography import x509
from typer.testing import CliRunner, Result

from orbit.agent import INVALID_CONFIG
from orbit.agent.external.certificates import CertsDir, fingerprint
from orbit.cli import app
from tests.tls_certs import DAY, Ca, make_server

runner = CliRunner()


def _certs(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["certs", *args, "--config-dir", str(config_dir)])


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _expiry(path: Path) -> str:
    return f"{_certificate(path).not_valid_after_utc:%Y-%m-%d}"


def _fingerprint(path: Path) -> str:
    return fingerprint(_certificate(path))


def _cells(result: Result) -> list[list[str]]:
    """The columns of each line ``result`` printed, which two or more spaces part."""
    return [re.split(" {2,}", line) for line in result.stdout.splitlines()]


def _revoke(config_dir: Path, *fingerprints: str) -> None:
    entries = ", ".join(f'"{entry}"' for entry in fingerprints)
    (config_dir / "config.toml").write_text(f"[server]\nrevoked = [{entries}]\n")


@pytest.fixture
def certs(config_dir: Path) -> CertsDir:
    """The CA and server certificate ``orbit certs init`` made in ``config_dir``, and the client
    certificate ``orbit certs client laptop`` made."""
    for args in (("init", "box"), ("client", "laptop")):
        result = _certs(config_dir, *args)
        assert result.exit_code == 0, result.output
    return CertsDir(config_dir)


def test_list_shows_the_ca_the_server_with_its_names_and_each_client(
    certs: CertsDir, config_dir: Path
) -> None:
    result = _certs(config_dir, "list")

    assert result.exit_code == 0, result.output
    laptop = certs.client("laptop")
    assert _cells(result) == [
        [
            "CA",
            f"orbit CA ({socket.gethostname()})",
            _expiry(certs.ca),
            "3650 days left",
            _fingerprint(certs.ca),
        ],
        [
            "server",
            "box (box, localhost, 127.0.0.1)",
            _expiry(certs.server),
            "365 days left",
            _fingerprint(certs.server),
        ],
        ["client", "laptop", _expiry(laptop), "365 days left", _fingerprint(laptop)],
    ]


def test_a_client_whose_fingerprint_is_revoked_is_marked_revoked(
    certs: CertsDir, config_dir: Path
) -> None:
    _revoke(config_dir, _fingerprint(certs.client("laptop")))
    result = _certs(config_dir, "list")

    assert result.exit_code == 0, result.output
    assert [cells[3] for cells in _cells(result)] == [
        "3650 days left",
        "365 days left",
        "365 days left, revoked",
    ]


def test_a_certificate_expiring_within_14_days_is_marked_expires_soon(
    certs: CertsDir, config_dir: Path
) -> None:
    _certs(config_dir, "client", "phone", "--days", "10")
    result = _certs(config_dir, "list")

    assert _cells(result)[-1][:4] == [
        "client",
        "phone",
        _expiry(certs.client("phone")),
        "10 days left, expires soon",
    ]


def test_an_expired_certificate_is_marked_expired(
    certs: CertsDir, config_dir: Path, ca: Ca
) -> None:
    make_server(ca, certs.path, valid_from=-30 * DAY, valid_for=20 * DAY, hosts=("box",))
    result = _certs(config_dir, "list")

    assert result.exit_code == 0, result.output
    assert _cells(result)[1][:4] == ["server", "localhost (box)", _expiry(certs.server), "expired"]


def test_files_set_aside_arent_listed(certs: CertsDir, config_dir: Path) -> None:
    _certs(config_dir, "server", "orbit.example.com")
    _certs(config_dir, "client", "laptop", "--force")
    result = _certs(config_dir, "list")

    assert [cells[:2] for cells in _cells(result)] == [
        ["CA", f"orbit CA ({socket.gethostname()})"],
        ["server", "orbit.example.com (orbit.example.com, localhost, 127.0.0.1)"],
        ["client", "laptop"],
    ]
    assert _cells(result)[2][4] == _fingerprint(certs.client("laptop"))


def test_without_certs_list_says_there_are_none_pointing_to_init(config_dir: Path) -> None:
    result = _certs(config_dir, "list")

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"There are no certificates in {CertsDir(config_dir).path}; make a CA and a server"
        " certificate with orbit certs init.\n"
    )


def test_list_with_an_invalid_config_is_refused(certs: CertsDir, config_dir: Path) -> None:
    _revoke(config_dir, "AB:CD")
    result = _certs(config_dir, "list")

    assert result.exit_code == 2
    assert result.stderr.startswith(f"{INVALID_CONFIG}")
    assert "'AB:CD' isn't a SHA-256 fingerprint" in result.stderr


def test_list_with_a_certificate_that_cant_be_read_is_refused_naming_it(
    certs: CertsDir, config_dir: Path
) -> None:
    certs.client("laptop").write_text("not a certificate")
    result = _certs(config_dir, "list")

    assert result.exit_code == 2
    assert result.stderr.startswith(f"Can't read {certs.client('laptop')}: it isn't a certificate")
