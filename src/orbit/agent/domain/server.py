"""Where ``orbit serve`` listens: the user's server settings, which hosts it may bind, how it
serves TLS, and what its run file says while it does."""

import ipaddress
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.protocol import DEFAULT_MAX_MESSAGE_MB

LOCALHOST = "localhost"

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


class ServerSettings(TlsSettings):
    """The user config's ``[server]``: the ``host`` and ``port`` ``orbit serve`` listens on (0
    lets the system choose one), ``max_message_mb``, the largest message either way, and its TLS
    settings."""

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=7433, ge=0, le=65535)
    max_message_mb: int = Field(default=DEFAULT_MAX_MESSAGE_MB, ge=1)

    @property
    def max_message_bytes(self) -> int:
        return self.max_message_mb * 1024 * 1024


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


class RunningServer(BaseModel):
    """A running ``orbit serve``, as its run file says: the ``url`` it listens at, in the
    process ``pid``."""

    model_config = ConfigDict(frozen=True, strict=True)

    url: str
    pid: int = Field(gt=0)


def parse_running(text: str) -> RunningServer | None:
    """The server a run file's JSON ``text`` names; ``None`` when it's unreadable."""
    try:
        return RunningServer.model_validate_json(text)
    except ValidationError:
        return None


def is_loopback(host: str) -> bool:
    """Whether ``host`` is ``localhost`` or a loopback address, reachable from this machine
    alone."""
    if host.lower() == LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
