"""Where ``orbit serve`` listens: the user's server settings, which hosts it may bind, how it
serves TLS, and what its run file says while it does."""

import hashlib
import ipaddress
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.protocol import DEFAULT_MAX_MESSAGE_MB
from orbit.protocol.transport import LOCALHOST, netloc
from orbit.protocol.transport import is_loopback as is_loopback

EXPIRY_WARNING = timedelta(days=14)
"""How long before a server certificate expires ``orbit serve`` warns of it."""

TlsMode = Literal["off", "tls", "mtls"]
"""How ``orbit serve`` serves: plaintext, TLS with a server certificate, or mutual TLS."""

_NEEDED: dict[TlsMode, tuple[str, ...]] = {
    "off": (),
    "tls": ("cert", "key"),
    "mtls": ("cert", "key", "client_ca"),
}


class TlsSettings(BaseModel):
    """How ``orbit serve`` serves TLS: its ``tls`` mode, the server certificate ``cert`` and its
    private ``key``, the ``client_ca`` that signs client certificates under mutual TLS, and the
    ``ca`` that signed ``cert``, each a path or ``None``."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tls: TlsMode = "off"
    cert: str | None = None
    key: str | None = None
    client_ca: str | None = None
    ca: str | None = None


def missing_tls_key(settings: TlsSettings) -> str | None:
    """The first key ``settings``' mode needs that isn't set; ``None`` if none is missing."""
    return next((key for key in _NEEDED[settings.tls] if getattr(settings, key) is None), None)


class CertificateDates(StrEnum):
    """Where now falls in a certificate's validity."""

    VALID = "valid"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    NOT_YET_VALID = "not yet valid"


def certificate_dates(not_before: datetime, not_after: datetime, now: datetime) -> CertificateDates:
    """Where ``now`` falls in a certificate valid from ``not_before`` to ``not_after``:
    expiring if it ends within :data:`EXPIRY_WARNING`."""
    if now < not_before:
        return CertificateDates.NOT_YET_VALID
    if now > not_after:
        return CertificateDates.EXPIRED
    if not_after - now < EXPIRY_WARNING:
        return CertificateDates.EXPIRING
    return CertificateDates.VALID


SERVER_PATHS = (
    "cert",
    "key",
    "client_ca",
    "ca",
    "local_client_cert",
    "local_client_key",
    "clone_dir",
)
"""The server settings that name files."""


class ServerSettings(TlsSettings):
    """The user config's ``[server]``: the ``host`` and ``port`` ``orbit serve`` listens on (0
    lets the system choose one), ``max_message_mb``, the largest message either way, its TLS
    settings, the fingerprints of the client certificates ``revoked`` under mutual TLS, and the
    client certificate ``local_client_cert``, with its private key ``local_client_key``, that
    frontends on this machine show it under mutual TLS, and the ``clone_dir`` the repositories
    it's asked to serve by URL are cloned into, ``workspaces`` in the state directory if
    ``None``."""

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=7433, ge=0, le=65535)
    max_message_mb: int = Field(default=DEFAULT_MAX_MESSAGE_MB, ge=1)
    revoked: tuple[str, ...] = Field(default=(), strict=False)  # A TOML array is a list.
    local_client_cert: str | None = None
    local_client_key: str | None = None
    clone_dir: str | None = None

    @field_validator("revoked")
    @classmethod
    def _sha256_fingerprints(cls, revoked: tuple[str, ...]) -> tuple[str, ...]:
        for entry in revoked:
            if not _SHA256.fullmatch(_bare(entry)):
                raise ValueError(
                    f"{entry!r} isn't a SHA-256 fingerprint: give all 64 hex digits, as orbit "
                    "serve's connect line or `openssl x509 -noout -fingerprint -sha256` shows it"
                )
        return revoked

    @property
    def max_message_bytes(self) -> int:
        return self.max_message_mb * 1024 * 1024


_SHA256 = re.compile(r"[0-9A-F]{64}")
"""A SHA-256 fingerprint without colons or spaces, in uppercase."""


def client_fingerprint(der: bytes) -> str:
    """The SHA-256 fingerprint of the certificate ``der``, in DER, as colon-separated
    uppercase hex."""
    return ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest())


def is_revoked(fingerprint: str, revoked: Iterable[str]) -> bool:
    """Whether ``fingerprint`` is among the ``revoked`` ones, which may be written in either
    case and with or without colons."""
    return _bare(fingerprint) in {_bare(entry) for entry in revoked}


def _bare(fingerprint: str) -> str:
    """``fingerprint`` without colons or spaces, in uppercase."""
    return fingerprint.replace(":", "").replace(" ", "").upper()


class _UserConfig(BaseModel):
    """The part of the user config the server reads; the rest is left to others."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    server: ServerSettings = ServerSettings()


def load_server(data: dict[str, Any]) -> ServerSettings:
    """The server settings in ``data``, a user config file's contents, over the defaults; its
    tables but ``[server]`` are ignored.

    Raises :class:`ConfigError` naming the key if ``[server]`` is invalid.
    """
    try:
        return _UserConfig.model_validate(data).server
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None


_WILDCARDS = {"0.0.0.0": (LOCALHOST, "127.0.0.1"), "::": (LOCALHOST, "::1", "127.0.0.1")}
"""The hosts that listen on every address, each with the names reaching it here: ``0.0.0.0``
listens on IPv4 only, and ``::`` is dual-stack."""


def local_hostname(host: str, names: Iterable[str]) -> str | None:
    """The name a frontend on this machine connects by to a server listening on ``host`` whose
    certificate gives ``names``: one of :func:`local_candidates` the certificate covers; ``None``
    if it covers none."""
    return next((c for c in local_candidates(host) if _named(c, names)), None)


def local_candidates(host: str) -> tuple[str, ...]:
    """The names a frontend on this machine may reach a server listening on ``host`` by, the
    first preferred."""
    if host in _WILDCARDS:
        return _WILDCARDS[host]
    if is_loopback(host):
        return (host, LOCALHOST)
    return (host,)


def _named(candidate: str, names: Iterable[str]) -> bool:
    """Whether a certificate giving ``names`` covers ``candidate``, a hostname or address."""
    return any(_covers(name, candidate) for name in names)


def _covers(name: str, candidate: str) -> bool:
    """Whether the certificate name ``name`` covers ``candidate``: the same address, the same
    hostname in any case, or a wildcard ``*.`` standing for its first label."""
    address, wanted = _address(name), _address(candidate)
    if address is not None or wanted is not None:
        return address == wanted
    name, candidate = name.lower(), candidate.lower()
    if name.startswith("*."):
        return candidate.partition(".")[2] == name[2:]
    return name == candidate


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP address; ``None`` if it's a hostname."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class LocalTls(BaseModel):
    """How a frontend on this machine reaches a server serving TLS: showing a client
    certificate if ``mutual``, trusting the CA certificate at ``ca``, and connecting by
    ``hostname``, which the server's certificate gives."""

    model_config = ConfigDict(frozen=True, strict=True)

    mutual: bool
    ca: str
    hostname: str


class RunningServer(BaseModel):
    """A running ``orbit serve``, as its run file says: the ``url`` it listens at, in the
    process ``pid``, and, if it serves TLS, how a frontend on this machine reaches it."""

    model_config = ConfigDict(frozen=True, strict=True)

    url: str
    pid: int = Field(gt=0)
    tls: LocalTls | None = None

    @property
    def local_url(self) -> str:
        """The URL a frontend on this machine connects to: :attr:`url`, by the hostname its TLS
        certificate gives, if any."""
        if self.tls is None:
            return self.url
        parts = urlsplit(self.url)
        assert parts.port is not None  # The run file's URL is serve's, which has a port.
        return parts._replace(netloc=netloc(self.tls.hostname, parts.port)).geturl()


def parse_running(text: str) -> RunningServer | None:
    """The server a run file's JSON ``text`` names; ``None`` when it's unreadable."""
    try:
        return RunningServer.model_validate_json(text)
    except ValidationError:
        return None
