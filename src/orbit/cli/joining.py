"""How a frontend reaches a control plane: the one it was asked to join, or the one found
running here, and the token or client certificate it shows it."""

import asyncio
import ssl
from dataclasses import dataclass, replace
from pathlib import Path

import typer

from orbit.agent import (
    USER_CONFIG_FILE,
    RemoteSettings,
    RunningServer,
    TokenFile,
    read_remotes,
    read_server,
    running_server,
)
from orbit.cli.errors import _configured, _exit_on
from orbit.client import (
    CaUnreadable,
    ClientCertUnusable,
    PlaintextRefused,
    TokenUnreadable,
    WebSocketSession,
    accepts,
    client_ssl_context,
    messages,
    read_token,
)
from orbit.protocol import NewSession


@dataclass(frozen=True)
class ClientFiles:
    """The files a frontend joining a control plane shows it: the token in ``token_file``, or
    the client certificate ``cert`` with its ``key``, verifying it by the CA in ``ca``; each may
    be ``None``."""

    token_file: Path | None = None
    ca: Path | None = None
    cert: Path | None = None
    key: Path | None = None

    def over(self, configured: ClientFiles) -> ClientFiles:
        """These files over the ``configured`` ones: the token or client certificate given, if
        any, in place of the configured one, and the CA given, if any."""
        return replace(self if self.identified else configured, ca=self.ca or configured.ca)

    @property
    def identified(self) -> bool:
        """Whether these files name a token or a client certificate to show."""
        return (self.token_file, self.cert, self.key) != (None, None, None)

    def token_path(self, config_dir: Path) -> Path:
        """The token file these files name, or else the one in the config directory
        ``config_dir``."""
        return self.token_file or TokenFile(config_dir).path


@dataclass(frozen=True)
class Target:
    """The control plane at ``url`` a frontend was asked to join, showing it ``files``;
    ``flag`` is how it was asked, --connect or --remote."""

    url: str
    files: ClientFiles
    flag: str


def named_target(
    connect: str | None, remote: str | None, config_dir: Path, given: ClientFiles
) -> Target | None:
    """The control plane --connect, ``connect``, or --remote, ``remote``, names, as the user
    config in ``config_dir`` says, with the ``given`` files over the remote's; ``None`` if
    neither was given.

    Exits with status 2 if both were given, or the remote isn't configured.
    """
    if remote is None:
        return None if connect is None else Target(_connect_url(connect), given, "--connect")
    if connect is not None:
        typer.echo(
            "--remote names the control plane to connect to, so it can't go with --connect.",
            err=True,
        )
        raise typer.Exit(2)
    configured = _remote(config_dir, remote)
    return Target(configured.url, given.over(_remote_files(configured)), "--remote")


def _connect_url(url: str) -> str:
    """The URL --connect gives, ``url``.

    Exits with status 2 if it isn't a ws:// or wss:// URL.
    """
    if not url.startswith(("ws://", "wss://")):
        typer.echo(f"--connect takes a wss:// or ws:// URL, not {url}.", err=True)
        raise typer.Exit(2)
    return url


def _remote(config_dir: Path, name: str) -> RemoteSettings:
    """The remote ``name`` in the user config in ``config_dir``.

    Exits with status 2 if the config is invalid, or names no such remote.
    """
    remotes = _configured(lambda: read_remotes(config_dir))
    if name in remotes:
        return remotes[name]
    typer.echo(_unknown_remote(name, sorted(remotes), config_dir / USER_CONFIG_FILE), err=True)
    raise typer.Exit(2)


def _unknown_remote(name: str, configured: list[str], path: Path) -> str:
    """What says there's no remote ``name`` in the user config at ``path``, listing the
    ``configured`` ones."""
    if not configured:
        return (
            f"No remote named {name}: none are configured in {path}; add a [remotes.{name}] table."
        )
    return f"No remote named {name} in {path}; the remotes configured are {', '.join(configured)}."


def _remote_files(remote: RemoteSettings) -> ClientFiles:
    """The files ``remote`` names."""
    paths = (remote.token_file, remote.ca, remote.cert, remote.key)
    return ClientFiles(*(None if path is None else Path(path) for path in paths))


@dataclass(frozen=True)
class Joining:
    """How a frontend joins a running control plane: with the flags in ``ctx``, showing the
    token in ``token_file``, or without one the client certificate at ``cert`` with its key at
    ``key``, verifying a ``wss://`` one by the CA in ``ca``, if given, its workspace picker
    preselecting, or offering to add, the workspace at the ``repository`` it runs in, and
    sending ``opening``, if any, once attached."""

    ctx: typer.Context
    token_file: Path | None
    ca: Path | None
    opening: NewSession | None
    cert: Path | None = None
    key: Path | None = None
    repository: str | None = None


def joining_for(
    ctx: typer.Context, files: ClientFiles, config_dir: Path, opening: NewSession | None
) -> Joining:
    """How a frontend with the flags in ``ctx`` joins a control plane showing it ``files``, or
    the token in the config directory ``config_dir`` without one, and sending ``opening``, if
    any.

    Exits with status 2 if a client certificate was given with a token file.
    """
    joining = Joining(ctx, files.token_path(config_dir), files.ca, opening)
    return _certified(joining, files.cert, files.key, files.token_file)


def _certified(
    joining: Joining, cert: Path | None, key: Path | None, token_file: Path | None
) -> Joining:
    """``joining`` showing the client certificate at ``cert`` with its key at ``key`` rather
    than the token, if either is given.

    Exits with status 2 if --token-file, ``token_file``, was given along with them.
    """
    if cert is None and key is None:
        return joining
    if token_file is not None:
        typer.echo(
            "--cert and --key show a client certificate rather than the token, so they can't go"
            " with --token-file.",
            err=True,
        )
        raise typer.Exit(2)
    return replace(joining, token_file=None, cert=cert, key=key)


def refuse_state_dir(ctx: typer.Context, flag: str) -> None:
    """Exit with status 2 if --state-dir was given to a frontend joining the control plane
    ``flag``, --connect or --remote, names, which keeps its own state and would ignore it."""
    if option_given(ctx, "state"):
        typer.echo(
            f"--state-dir says where a control plane run here keeps its state, so it can't go"
            f" with {flag}.",
            err=True,
        )
        raise typer.Exit(2)


def option_given(ctx: typer.Context, name: str) -> bool:
    """Whether the option ``name`` was given, not left at its default."""
    source = ctx.get_parameter_source(name)  # Typer's own click, so its enum is compared by name.
    return source is not None and source.name != "DEFAULT"


class LocalCertMissing(Exception):
    """A control plane on this machine serves mutual TLS, and the user config names no client
    certificate to show it; the message names the keys to set."""


@dataclass(frozen=True)
class Discovered:
    """A control plane found running here: the ``url`` a frontend here connects to, showing it
    ``files``."""

    url: str
    files: ClientFiles


def discover_control_plane(state: Path, given: ClientFiles, config_dir: Path) -> Discovered | None:
    """The control plane running on the ``state`` directory, as its run file there says, if
    the process it names is alive and it takes the files ``given`` over those it would be shown
    otherwise: the token in the config directory ``config_dir``, or under mutual TLS the client
    certificate the user config there names; ``None`` if there's none, or it can't be reached
    at all.

    Raises :class:`TokenUnreadable` if the token can't be read, :class:`TokenRejected` if the
    control plane turns it away, :class:`ConfigError` if the user config is invalid,
    :class:`LocalCertMissing` if it names no client certificate under mutual TLS, and
    :class:`CaUnreadable` or :class:`ClientCertUnusable` if the CA or the client certificate
    can't be used, and :class:`HandshakeRefused` if it can't be reached safely, as its TLS
    handshake fails in a way that would fail again.
    """
    server = running_server(state)
    if server is None:
        return None
    files = _local_files(server, given, config_dir)
    url = server.local_url
    context = None if server.tls is None else client_ssl_context(files.ca, files.cert, files.key)
    token = None if files.token_file is None else read_token(files.token_file)
    found = asyncio.run(accepts(url, token, context))
    return Discovered(url, files) if found else None


def _local_files(server: RunningServer, given: ClientFiles, config_dir: Path) -> ClientFiles:
    """The files a frontend on this machine shows ``server``: those ``given`` over the token in
    the config directory ``config_dir``, verifying it by the CA its run file names over TLS, or
    under mutual TLS the client certificate in the user config there instead of the token.

    Raises :class:`ConfigError` if the user config is invalid, and :class:`LocalCertMissing`
    if it names no client certificate under mutual TLS, and none is given.
    """
    ca = None if server.tls is None else Path(server.tls.ca)
    if given.identified:
        return given.over(ClientFiles(ca=ca))
    if server.tls is None or not server.tls.mutual:
        return given.over(ClientFiles(TokenFile(config_dir).path, ca))
    return given.over(replace(_local_cert(server, config_dir), ca=ca))


def _local_cert(server: RunningServer, config_dir: Path) -> ClientFiles:
    """The client certificate the user config in ``config_dir`` names for frontends on this
    machine to show ``server``.

    Raises :class:`ConfigError` if the user config is invalid, and :class:`LocalCertMissing`
    if it names no client certificate.
    """
    settings = read_server(config_dir)
    cert, key = settings.local_client_cert, settings.local_client_key
    if cert is None or key is None:
        path = config_dir / USER_CONFIG_FILE
        raise LocalCertMissing(messages.LOCAL_CLIENT_CERT_MISSING.format(url=server.url, path=path))
    return ClientFiles(cert=Path(cert), key=Path(key))


LOCAL_ERRORS = (
    TokenUnreadable,
    LocalCertMissing,
    CaUnreadable,
    ClientCertUnusable,
)
"""What stops a frontend finding a control plane on this machine, said as it is."""


def joined_client(url: str, joining: Joining) -> WebSocketSession:
    """The session with the control plane at ``url``, shown the token or client certificate
    ``joining`` gives, verified by the CA it gives, if any.

    Exits with status 2 if ``url`` is a ws:// one beyond loopback, or the token, the CA or the
    client certificate can't be read.
    """
    with _exit_on(TokenUnreadable, CaUnreadable, ClientCertUnusable, PlaintextRefused):
        if joining.token_file is not None:
            read_token(joining.token_file)  # The session reads it again for each connection.
        context = _ssl_context(url, joining)
        return WebSocketSession(url, joining.token_file, ssl_context=context)


def _ssl_context(url: str, joining: Joining) -> ssl.SSLContext | None:
    """What verifies the control plane at ``url``, trusting the CA ``joining`` gives if any,
    and shows the client certificate it gives, if any: none for a ``ws://`` URL, which has no
    TLS.

    Raises :class:`CaUnreadable` if the CA can't be read, and :class:`ClientCertUnusable` if
    the client certificate can't be used.
    """
    if not url.startswith("wss://"):
        return None
    return client_ssl_context(joining.ca, joining.cert, joining.key)
