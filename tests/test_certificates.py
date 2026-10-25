"""orbit's own CA, the server and client certificates it signs, and the files in ``certs`` in the
config directory that hold them."""

import asyncio
import ipaddress
import re
import ssl
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    KeySerializationEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from websockets.asyncio.client import connect

from orbit.agent import ControlPlane, WorkspaceHost
from orbit.agent.domain.certificate_list import CertificateKind, ListedCertificate
from orbit.agent.domain.server import ServerSettings, client_fingerprint
from orbit.agent.external.certificates import (
    CertificateError,
    CertsDir,
    KeyType,
    PrivateKey,
    fingerprint,
    issue_client,
    issue_server,
    make_ca,
    read_certificate,
)
from orbit.agent.external.tls import tls_warnings
from orbit.protocol import PROTOCOL_VERSION, CommandEnvelope, Hello, Welcome, decode_event, encode
from tests.server_fakes import Lines, serving_plane

NAMES = ("orbit.example.com", "10.0.0.5")
LONG_NAME = f"{'x' * 88}.example.com"  # 100 characters, more than a common name's 64.
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


@pytest.mark.parametrize(
    "name",
    [
        "",
        "two words",
        "héllo.example.com",
        "orbit/serve",
        "-",
        ".",
        "a..b",
        "a*b",
        "-box.example.com",
        "box-.example.com",
        "*",
        "*.",
        "box.*.com",
        "box.example.com..",
    ],
)
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


@pytest.mark.parametrize(
    "name", ["*.example.com", "my_host.lan", "box.example.com.", "a-b.c_d.example", "box"]
)
def test_a_hostname_of_labels_a_wildcard_first_or_a_trailing_dot_is_held_as_a_dns_name(
    ca: tuple[x509.Certificate, PrivateKey], name: str
) -> None:
    certificate, _ = issue_server(*ca, [name])
    assert _alternative_names(certificate)[0] == x509.DNSName(name)
    assert _common_name(certificate.subject) == name


def test_a_ca_of_a_long_hostname_is_named_for_as_much_of_it_as_fits() -> None:
    hostname = "box-" * 15
    certificate, _ = make_ca(hostname)
    assert _common_name(certificate.subject) == f"orbit CA ({hostname[:50]}…)"
    assert len(_common_name(certificate.subject).encode()) == 64
    certificate.verify_directly_issued_by(certificate)


def test_a_ca_of_a_hostname_that_just_fits_is_named_for_all_of_it() -> None:
    certificate, _ = make_ca("h" * 53)
    assert _common_name(certificate.subject) == f"orbit CA ({'h' * 53})"


def test_a_server_certificate_of_a_long_first_name_is_named_for_its_start_holding_it_whole(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_server(*ca, [LONG_NAME, "10.0.0.5"])
    assert _common_name(certificate.subject) == f"{'x' * 61}…"
    assert _alternative_names(certificate)[:2] == [
        x509.DNSName(LONG_NAME),
        x509.IPAddress(ipaddress.ip_address("10.0.0.5")),
    ]
    certificate.verify_directly_issued_by(ca[0])


def test_a_client_certificate_of_a_long_name_is_named_for_its_start(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_client(*ca, "c" * 100)
    assert _common_name(certificate.subject) == f"{'c' * 61}…"


def test_a_client_certificate_of_a_name_that_just_fits_is_named_for_all_of_it(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_client(*ca, "c" * 64)
    assert _common_name(certificate.subject) == "c" * 64


def test_a_long_name_is_cut_between_letters_not_inside_one(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_client(*ca, "é" * 40)
    assert _common_name(certificate.subject) == f"{'é' * 30}…"


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


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".laptop",
        "-laptop",
        "../laptop",
        "lap/top",
        "two words",
        "läptop",
        "\u212alaptop",
        "a" * 65,
        "laptop-key",
        "laptop-KEY",
    ],
)
def test_a_client_name_that_isnt_a_safe_file_name_is_refused(tmp_path: Path, name: str) -> None:
    certs = CertsDir(tmp_path)
    message = (
        f'Can\'t name a client "{name}": its files are named for it, so give up to 64 letters,'
        " digits, dots, dashes and underscores, starting with a letter or digit and not ending in"
        " -key, such as laptop; laptop-key.pem is laptop's key, so a client named laptop-key"
        " would share it."
    )
    with pytest.raises(CertificateError, match=f"^{re.escape(message)}$"):
        certs.client(name)
    with pytest.raises(CertificateError, match=f"^{re.escape(message)}$"):
        certs.client_key(name)


@pytest.mark.parametrize(
    "name",
    [
        "laptop",
        "Work-Laptop_2.home",
        "7",
        "a" * 64,
        "keyboard",
        "monkey",
        "key-laptop",
        "laptop-keyboard",
    ],
)
def test_a_client_name_of_letters_digits_dots_dashes_and_underscores_is_taken(
    tmp_path: Path, name: str
) -> None:
    certs = CertsDir(tmp_path)
    assert certs.client(name) == certs.path / "clients" / f"{name}.pem"
    assert certs.client_key(name) == certs.path / "clients" / f"{name}-key.pem"


def _pem(key: PrivateKey, encryption: KeySerializationEncryption | None = None) -> bytes:
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, encryption or serialization.NoEncryption()
    )


def _with_ca(certs: CertsDir, ca: tuple[x509.Certificate, PrivateKey]) -> None:
    certs.write(certs.ca, ca[0])
    certs.write_key(certs.ca_key, ca[1])


def test_a_certificate_is_read_from_its_pem(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.server, ca[0])
    assert read_certificate(certs.server) == ca[0]


def test_a_file_that_isnt_a_pem_certificate_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "laptop.pem"
    path.write_text("not a certificate")
    with pytest.raises(CertificateError) as refused:
        read_certificate(path)
    assert str(refused.value) == (
        f"Can't read {path}: it isn't a certificate in PEM. Put back the one orbit certs wrote"
        " there, or move it away."
    )


def test_a_file_that_cant_be_read_is_refused_naming_it(tmp_path: Path) -> None:
    with pytest.raises(CertificateError) as refused:
        read_certificate(tmp_path)
    assert str(refused.value) == (
        f"Can't read {tmp_path}: Is a directory. Check that you can read it."
    )


def test_a_fingerprint_is_the_certificates_sha256_as_orbit_serve_shows_it(
    ca: tuple[x509.Certificate, PrivateKey],
) -> None:
    certificate, _ = issue_client(*ca, "laptop")
    shown = fingerprint(certificate)
    assert shown == client_fingerprint(certificate.public_bytes(Encoding.DER))
    assert re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){31}", shown)


@pytest.mark.parametrize("key_type", ["ed25519", "p256"])
def test_the_issuer_is_the_ca_in_certs_and_its_key(tmp_path: Path, key_type: KeyType) -> None:
    ca = make_ca("box", key_type)
    certs = CertsDir(tmp_path)
    _with_ca(certs, ca)
    certificate, key = certs.issuer()
    assert certificate == ca[0]
    assert key.public_key() == ca[1].public_key()


def test_the_issuers_key_is_read_from_where_its_given(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path / "config")
    certs.write(certs.ca, ca[0])
    (tmp_path / "offline.pem").write_bytes(_pem(ca[1]))
    _, key = certs.issuer(tmp_path / "offline.pem")
    assert key.public_key() == ca[1].public_key()


def test_the_issuers_key_may_be_given_from_the_home_directory(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    certs = CertsDir(tmp_path / "config")
    certs.write(certs.ca, ca[0])
    (tmp_path / "offline.pem").write_bytes(_pem(ca[1]))
    _, key = certs.issuer(Path("~/offline.pem"))
    assert key.public_key() == ca[1].public_key()


def test_without_a_ca_certificate_the_issuer_is_refused_pointing_to_init(tmp_path: Path) -> None:
    certs = CertsDir(tmp_path)
    with pytest.raises(CertificateError) as refused:
        certs.issuer()
    assert str(refused.value) == (
        f"There's no CA certificate at {certs.ca}; make a CA with orbit certs init."
    )


def test_without_the_cas_key_the_issuer_is_refused_naming_ca_key(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.ca, ca[0])
    for given, missing in ((None, certs.ca_key), (tmp_path / "gone.pem", tmp_path / "gone.pem")):
        with pytest.raises(CertificateError) as refused:
            certs.issuer(given)
        assert str(refused.value) == (
            f"The CA's key isn't at {missing}; pass --ca-key with where it is."
        )


def test_a_key_that_isnt_the_cas_is_refused_naming_ca_key(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    _with_ca(certs, (ca[0], make_ca("other")[1]))
    with pytest.raises(CertificateError) as refused:
        certs.issuer()
    assert str(refused.value) == (
        f"{certs.ca_key} isn't the key of the CA at {certs.ca}; pass --ca-key with the CA's own"
        " key."
    )


@pytest.mark.parametrize(
    "pem",
    [
        b"not a key",
        _pem(ed25519.Ed25519PrivateKey.generate(), BestAvailableEncryption(b"secret")),
        rsa.generate_private_key(65537, 2048).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    ],
    ids=["text", "encrypted", "rsa"],
)
def test_a_ca_key_orbit_cant_sign_with_is_refused_naming_ca_key(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path, pem: bytes
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.ca, ca[0])
    certs.ca_key.write_bytes(pem)
    with pytest.raises(CertificateError) as refused:
        certs.issuer()
    assert str(refused.value) == (
        f"Can't read {certs.ca_key}: it isn't an Ed25519 or ECDSA private key in PEM without a"
        " passphrase. Pass --ca-key with the CA's key in that form."
    )


def test_a_ca_certificate_that_isnt_pem_is_refused_naming_it(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    _with_ca(certs, ca)
    certs.ca.write_text("not a certificate")
    with pytest.raises(CertificateError, match="it isn't a certificate in PEM"):
        certs.issuer()


def _listed(
    certificate: x509.Certificate,
    kind: CertificateKind,
    name: str,
    names: tuple[str, ...] = (),
) -> ListedCertificate:
    return ListedCertificate(
        kind=kind,
        name=name,
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        fingerprint=fingerprint(certificate),
        names=names,
    )


def test_the_certificates_listed_are_the_cas_the_servers_and_each_clients_in_order(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    server, _ = issue_server(*ca, NAMES)
    laptop, _ = issue_client(*ca, "laptop")
    desktop, _ = issue_client(*ca, "desktop")
    certs.write(certs.client("laptop"), laptop)
    certs.write(certs.server, server)
    certs.write(certs.ca, ca[0])
    certs.write(certs.client("desktop"), desktop)

    assert certs.listed() == (
        _listed(ca[0], "CA", "orbit CA (box)"),
        _listed(server, "server", "orbit.example.com", (*NAMES, "localhost", "127.0.0.1")),
        _listed(desktop, "client", "desktop"),
        _listed(laptop, "client", "laptop"),
    )


def test_a_client_certificate_is_listed_by_the_name_of_its_file(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certificate, _ = issue_client(*ca, "workstation")
    certs.write(certs.client("desktop"), certificate)

    assert certs.listed() == (_listed(certificate, "client", "desktop"),)


def test_keys_and_files_set_aside_arent_listed(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.ca, make_ca("old")[0])
    certs.set_aside([certs.ca])
    _with_ca(certs, ca)
    laptop, key = issue_client(*ca, "laptop")
    certs.write(certs.client("laptop"), laptop)
    certs.write_key(certs.client_key("laptop"), key)

    assert certs.listed() == (
        _listed(ca[0], "CA", "orbit CA (box)"),
        _listed(laptop, "client", "laptop"),
    )


def test_without_certs_no_certificates_are_listed(tmp_path: Path) -> None:
    assert CertsDir(tmp_path).listed() == ()


def _without_names(attribute: x509.NameAttribute[str]) -> x509.Certificate:
    """A certificate holding no names, of the subject with ``attribute`` alone, that signs
    itself."""
    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([attribute])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW)
        .not_valid_after(NOW + timedelta(days=1))
        .sign(key, None)
    )


def test_a_certificate_without_a_common_name_or_names_is_listed_by_its_subject(
    tmp_path: Path,
) -> None:
    certificate = _without_names(x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example"))
    certs = CertsDir(tmp_path)
    certs.write(certs.server, certificate)

    assert certs.listed() == (_listed(certificate, "server", "O=Example"),)


def test_the_server_name_is_the_whole_first_name_its_certificate_holds(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.server, issue_server(*ca, [LONG_NAME, "10.0.0.5"])[0])

    assert certs.server_name() == LONG_NAME


def test_the_server_name_of_a_certificate_holding_no_names_is_its_common_name(
    tmp_path: Path,
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.server, _without_names(x509.NameAttribute(NameOID.COMMON_NAME, "box")))

    assert certs.server_name() == "box"


def test_a_file_listed_that_isnt_a_certificate_is_refused_naming_it(
    ca: tuple[x509.Certificate, PrivateKey], tmp_path: Path
) -> None:
    certs = CertsDir(tmp_path)
    certs.write(certs.client("laptop"), ca[0])
    certs.client("laptop").write_text("not a certificate")

    with pytest.raises(CertificateError) as refused:
        certs.listed()

    assert str(refused.value).startswith(f"Can't read {certs.client('laptop')}: it isn't a")


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
