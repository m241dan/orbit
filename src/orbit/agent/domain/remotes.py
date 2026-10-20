"""The control planes ``orbit chat --remote`` connects to by name: the user config's
``[remotes.<name>]`` tables."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from orbit.agent.domain.config_errors import ConfigError, validation_problems

REMOTE_PATHS = ("ca", "token_file", "cert", "key")
"""The settings of a remote that name files."""


class RemoteSettings(BaseModel):
    """A ``[remotes.<name>]`` table: the ``url`` of the control plane, the ``ca`` that verifies
    it, and either the ``token_file`` to show it or the client certificate ``cert`` with its
    ``key``, each a path or ``None``."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    url: str = Field(pattern=r"^wss?://")
    ca: str | None = None
    token_file: str | None = None
    cert: str | None = None
    key: str | None = None

    @model_validator(mode="after")
    def _one_credential(self) -> Self:
        problem = credential_problem(self)
        if problem is not None:
            raise PydanticCustomError("remote_credentials", problem)
        return self


def credential_problem(remote: RemoteSettings) -> str | None:
    """What's wrong with how ``remote`` identifies itself, if anything: a certificate without
    its key or a key without its certificate, or both with a token file."""
    if (remote.cert is None) != (remote.key is None):
        if remote.key is None:
            return "cert needs key, the client certificate's key"
        return "key needs cert, the client certificate"
    if remote.cert is not None and remote.token_file is not None:
        return (
            "cert and key show a client certificate rather than the token, so they can't go"
            " with token_file"
        )
    return None


class _UserConfig(BaseModel):
    """The part of the user config the remotes are in; the rest is left to others."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    remotes: dict[str, RemoteSettings] = {}


def load_remotes(data: dict[str, Any]) -> dict[str, RemoteSettings]:
    """The remotes in ``data``, a user config file's contents, by name; its tables but
    ``[remotes]`` are ignored.

    Raises :class:`ConfigError` naming the key if a remote is invalid.
    """
    try:
        return _UserConfig.model_validate(data).remotes
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
