"""orbit's own CA, the server and client certificates it signs, and the files in ``certs`` in the
config directory that hold them."""

import asyncio
import ipaddress
import ssl
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from websockets.asyncio.client import connect

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.domain.server import ServerSettings
from orbit.agent.external.certificates import (
    CertificateError,
    CertsDir,
    KeyType,
    PrivateKey,
    issue_client,
    issue_server,
    make_ca,
)
from orbit.agent.external.tls import tls_warnings
from orbit.protocol import PROTOCOL_VERSION, CommandEnvelope, Hello, Welcome, decode_event, encode
from tests.server_fakes import Lines, serving_plane

NAMES = ("orbit.example.com", "10.0.0.5")
NOW = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)


def _common_name(name: x509.Name) -> str:
    [attribute] = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attribute.value)


def _extension[T: x509.ExtensionType](
    certificate: x509.Certificate, kind: type[T]
) -> x509.Extension[T]:
    return certificate.extensions.get_extension_for_class(kind)


def _lifetime(certificate: x509.Certificate) -> timedelta:
    return certificate.not_valid_after_utc - certificate.not_valid_before_utc


def _alternative_names(certificate: x509.Certificate) -> list[x509.GeneralName]:
    return list(_extension(certificate, x509.SubjectAlternativeName).value)


def _usages(certificate: x509.Certificate) -> list[x509.ObjectIdentifier]:
    return list(_extension(certificate, x509.ExtendedKeyUsage).value)


@pytest.fixture
def ca() -> tuple[x509.Certificate, PrivateKey]:
    return make_ca("box")


def test_a_ca_is_named_for_its_machine_and_signs_itself(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = ca
    assert _common_name(certificate.subject) == "orbit CA (box)"
    assert certificate.issuer == certificate.subject
    certificate.verify_directly_issued_by(certificate)


def test_a_ca_is_marked_a_ca_that_signs_certificates(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = ca
    constraints = _extension(certificate, x509.BasicConstraints)
    usage = _extension(certificate, x509.KeyUsage)
    assert constraints.critical and constraints.value.ca
    assert usage.critical and usage.value.key_cert_sign


def test_a_ca_is_valid_from_now_for_ten_years_with_an_ed25519_key(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, key = ca
    now = datetime.now(UTC)
    assert now - timedelta(minutes=1) < certificate.not_valid_before_utc <= now
    assert _lifetime(certificate) == timedelta(days=3650)
    assert isinstance(key, ed25519.Ed25519PrivateKey)


def test_a_p256_ca_has_an_ecdsa_key_and_signs_with_sha256() -> None:
    certificate, key = make_ca("box", "p256")
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert isinstance(key.curve, ec.SECP256R1)
    assert isinstance(certificate.signature_hash_algorithm, hashes.SHA256)
    certificate.verify_directly_issued_by(certificate)


def test_a_ca_lasts_the_days_its_given() -> None:
    certificate, _ = make_ca("box", days=30)
    assert _lifetime(certificate) == timedelta(days=30)


def test_a_server_certificate_is_named_for_its_first_name_and_signed_by_the_ca(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_server(*ca, NAMES)
    assert _common_name(certificate.subject) == "orbit.example.com"
    assert certificate.issuer == ca[0].subject
    certificate.verify_directly_issued_by(ca[0])


def test_a_server_certificate_holds_its_names_then_localhost_and_127_0_0_1(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_server(*ca, NAMES)
    assert _alternative_names(certificate) == [
        x509.DNSName("orbit.example.com"),
        x509.IPAddress(ipaddress.ip_address("10.0.0.5")),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]


def test_a_server_certificate_holds_each_name_once(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_server(*ca, ["127.0.0.1", "box", "box", "localhost"])
    assert _common_name(certificate.subject) == "127.0.0.1"
    assert _alternative_names(certificate) == [
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.DNSName("box"),
        x509.DNSName("localhost"),
    ]


def test_a_server_certificate_is_for_server_authentication_alone(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_server(*ca, NAMES)
    assert _usages(certificate) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert not _extension(certificate, x509.BasicConstraints).value.ca


def test_a_server_certificate_is_valid_from_now_for_a_year_with_an_ed25519_key(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, key = issue_server(*ca, NAMES)
    now = datetime.now(UTC)
    assert now - timedelta(minutes=1) < certificate.not_valid_before_utc <= now
    assert _lifetime(certificate) == timedelta(days=365)
    assert isinstance(key, ed25519.Ed25519PrivateKey)


def test_a_server_certificate_lasts_the_days_its_given_with_the_key_type_its_given(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, key = issue_server(*ca, NAMES, "p256", 30)
    assert _lifetime(certificate) == timedelta(days=30)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    certificate.verify_directly_issued_by(ca[0])


def test_certificates_have_serial_numbers_of_their_own(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    serials = {issue_server(*ca, NAMES)[0].serial_number for _ in range(3)}
    assert len(serials | {ca[0].serial_number}) == 4


@pytest.mark.parametrize("name", ["", "two words", "héllo.example.com", "orbit/serve"])
def test_a_name_that_is_neither_a_hostname_nor_an_address_is_refused_naming_it(
    ca: tuple[x509.Certificate, PrivateKey], name: str
) -> None:
    with pytest.raises(CertificateError) as refused:
        issue_server(*ca, ["box", name])
    assert str(refused.value) == (
        f"Can't make a certificate for \"{name}\": it's neither a hostname nor an IP address."
        " Give names such as orbit.example.com or 10.0.0.5, an internationalized name in its"
        " xn-- form."
    )


def test_a_client_certificate_is_named_for_its_client_for_client_authentication(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, key = issue_client(*ca, "laptop")
    assert _common_name(certificate.subject) == "laptop"
    assert _usages(certificate) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert _lifetime(certificate) == timedelta(days=365)
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    certificate.verify_directly_issued_by(ca[0])


def test_a_client_certificate_lasts_the_days_its_given_with_the_key_type_its_given(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, key = issue_client(*ca, "laptop", "p256", 30)
    assert _lifetime(certificate) == timedelta(days=30)
    assert isinstance(key, ec.EllipticCurvePrivateKey)


def test_the_files_are_in_certs_in_the_config_directory(tmp_path: Path) -> None:
    certs = CertsDir(tmp_path)
    assert certs.path == tmp_path / "certs"
    assert [certs.ca, certs.ca_key, certs.server, certs.server_key] == [
        tmp_path / "certs" / name
        for name in ("ca.pem", "ca-key.pem", "server.pem", "server-key.pem")
    ]
    assert certs.client("laptop") == tmp_path / "certs" / "clients" / "laptop.pem"
    assert certs.client_key("laptop") == tmp_path / "certs" / "clients" / "laptop-key.pem"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_certificate_is_written_in_pem_readable_by_all_in_an_owner_only_directory(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path / "config")
    certs.write(certs.ca, ca[0])
    assert x509.load_pem_x509_certificate(certs.ca.read_bytes()) == ca[0]
    assert _mode(certs.ca) == 0o644
    assert _mode(certs.path) == 0o700


def test_a_key_is_written_in_unencrypted_pem_readable_by_its_owner_alone(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write_key(certs.client_key("laptop"), ca[1])
    loaded = load_pem_private_key(certs.client_key("laptop").read_bytes(), password=None)
    assert isinstance(loaded, ed25519.Ed25519PrivateKey)
    assert loaded.public_key() == ca[1].public_key()
    assert _mode(certs.client_key("laptop")) == 0o600
    assert _mode(certs.path) == _mode(certs.path / "clients") == 0o700


def test_a_file_written_replaces_the_one_there_whole(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.server, ca[0])
    other, _ = make_ca("other")
    certs.write(certs.server, other)
    assert x509.load_pem_x509_certificate(certs.server.read_bytes()) == other
    assert sorted(p.name for p in certs.path.iterdir()) == ["server.pem"]


def test_a_file_that_cant_be_written_is_refused_naming_it_and_the_fix(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    config = tmp_path / "config"
    config.write_text("not a directory")
    certs = CertsDir(config)
    with pytest.raises(CertificateError) as refused:
        certs.write_key(certs.ca_key, ca[1])
    assert str(refused.value) == (
        f"Can't write {certs.ca_key}: Not a directory. Check that you can write to"
        f" {certs.path}, or give another config directory with --config-dir."
    )


def _written(*paths: Path) -> list[Path]:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name)
    return list(paths)


def test_files_set_aside_move_to_an_owner_only_directory_named_for_the_time(
    tmp_path: Path,
) -> None:
    certs = CertsDir(tmp_path)
    paths = _written(certs.ca, certs.server, certs.client("laptop"))
    old = certs.set_aside(paths, NOW)
    assert old == certs.path / "old-20261025T093000Z"
    assert sorted(p.name for p in old.iterdir()) == ["ca.pem", "laptop.pem", "server.pem"]
    assert (old / "laptop.pem").read_text() == "laptop.pem"
    assert not any(path.exists() for path in paths)
    assert _mode(old) == 0o700


def test_files_set_aside_in_the_same_second_go_to_a_directory_of_their_own(
    tmp_path: Path,
) -> None:
    certs = CertsDir(tmp_path)
    first = certs.set_aside(_written(certs.ca), NOW)
    second = certs.set_aside(_written(certs.ca), NOW)
    third = certs.set_aside(_written(certs.ca), NOW)
    assert [first.name, second.name, third.name] == [
        "old-20261025T093000Z",
        "old-20261025T093000Z-2",
        "old-20261025T093000Z-3",
    ]


def test_files_are_set_aside_at_the_present_by_default(tmp_path: Path) -> None:
    certs = CertsDir(tmp_path)
    before = datetime.now(UTC).replace(microsecond=0)
    old = certs.set_aside(_written(certs.ca))
    stamp = datetime.strptime(old.name, "old-%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    assert before <= stamp <= datetime.now(UTC)


def test_a_file_that_cant_be_set_aside_is_refused_naming_it(tmp_path: Path) -> None:
    certs = CertsDir(tmp_path)
    certs.path.mkdir()
    with pytest.raises(CertificateError) as refused:
        certs.set_aside([certs.ca], NOW)
    old = certs.path / "old-20261025T093000Z"
    assert str(refused.value) == (
        f"Can't move {certs.ca} to {old}: No such file or directory. Check that you can write"
        f" to {certs.path}."
    )


def test_a_directory_to_set_aside_in_that_cant_be_made_is_refused_naming_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").write_text("not a directory")
    certs = CertsDir(tmp_path / "config")
    with pytest.raises(CertificateError) as refused:
        certs.set_aside([], NOW)
    old = certs.path / "old-20261025T093000Z"
    assert str(refused.value) == (
        f"Can't make {old}: Not a directory. Check that you can write to {certs.path}."
    )


async def _welcomed(url: str, context: ssl.SSLContext) -> bool:
    """Whether a frontend showing no token over ``context`` is welcomed at ``url``."""
    hello = CommandEnvelope(
        command_id="hello", payload=Hello(client="tests", protocol_version=PROTOCOL_VERSION)
    )
    async with connect(url, ssl=context, proxy=None) as websocket:
        await websocket.send(encode(hello))
        envelope = decode_event(str(await asyncio.wait_for(websocket.recv(), 5)))
        return envelope is not None and type(envelope.payload) is Welcome


def _mtls_files(certs: CertsDir, key_type: KeyType) -> ServerSettings:
    """The settings serving mutual TLS with a CA, a server certificate for ``localhost`` and a
    certificate for the client ``laptop``, of ``key_type`` keys, written to ``certs``."""
    ca, ca_key = make_ca("box", key_type)
    server, server_key = issue_server(ca, ca_key, ["box"], key_type)
    client, client_key = issue_client(ca, ca_key, "laptop", key_type)
    certs.write(certs.ca, ca)
    certs.write(certs.server, server)
    certs.write_key(certs.server_key, server_key)
    certs.write(certs.client("laptop"), client)
    certs.write_key(certs.client_key("laptop"), client_key)
    return ServerSettings(
        port=0,
        tls="mtls",
        cert=str(certs.server),
        key=str(certs.server_key),
        client_ca=str(certs.ca),
    )


@pytest.mark.parametrize("key_type", ["ed25519", "p256"])
async def test_orbit_serve_serves_mutual_tls_to_a_client_with_the_certificates_made(
    tmp_path: Path, key_type: KeyType
) -> None:
    certs = CertsDir(tmp_path)
    settings = _mtls_files(certs, key_type)
    assert tls_warnings(settings) == ()
    context = ssl.create_default_context(cafile=certs.ca)
    context.load_cert_chain(certs.client("laptop"), certs.client_key("laptop"))
    served = ControlPlane([WorkspaceHost()])
    lines = Lines()
    async with serving_plane(served, served.close, settings, lambda: "", lines) as (url, _):
        assert await _welcomed(url.replace("127.0.0.1", "localhost"), context)
    assert any(" as CN=laptop (" in line for line in lines.said), lines.said
