"""Where ``orbit serve`` listens: the user's server settings, which hosts it may bind, and what
its run file says while it does."""

import ipaddress
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orbit.agent.domain.config_errors import ConfigError, validation_problems

LOCALHOST = "localhost"


class ServerSettings(BaseModel):
    """The user config's ``[server]``: the ``host`` and ``port`` ``orbit serve`` listens on (0
    lets the system choose one), and ``max_message_mb``, the largest message either way."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=7433, ge=0, le=65535)
    max_message_mb: int = Field(default=64, ge=1)

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
