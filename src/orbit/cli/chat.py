"""orbit chat: the TUI over a control plane run here, or one it joins."""

import asyncio
import os
import ssl
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import typer

from orbit.agent import (
    USER_CONFIG_FILE,
    RemoteSettings,
    RunningServer,
    TokenFile,
    read_remotes,
    read_server,
    resolve_config_dir,
    resolve_state_dir,
    running_server,
)
from orbit.cli.backend import _Backend, _backend, _Directories, _in_process, _workspace
from orbit.cli.errors import _configured, _exit_on
from orbit.cli.options import (
    _SERVE_FLAGS,
    CaOption,
    ClientCertOption,
    ClientKeyOption,
    ConfigDirOption,
    ConnectOption,
    LocalOption,
    ModelOption,
    NameOption,
    NewOption,
    ProfileOption,
    RemoteOption,
    ReviewOption,
    StateDirOption,
    TokenFileOption,
    ToolsOption,
    UsageOption,
    VerifyOption,
    WorkspaceOption,
    WorktreeOption,
)
from orbit.cli.telemetry import _agent_collector, _refuse_console, _telemetry
from orbit.cli.threaded import ThreadedSession
from orbit.client import (
    CaUnreadable,
    ClientCertUnusable,
    HandshakeRefused,
    PlaintextRefused,
    TokenRejected,
    TokenUnreadable,
    WebSocketSession,
    accepts,
    client_ssl_context,
    messages,
    read_token,
)
from orbit.profiling import profiled
from orbit.protocol import NewSession
from orbit.protocol.transport import is_loopback
from orbit.telemetry import TelemetryRun
from orbit.tracing import Telemetry
from orbit.tui import Opening, OrbitApp


def chat(
    ctx: typer.Context,
    workspace: WorkspaceOption = Path("."),
    model: ModelOption = None,
    tools: ToolsOption = None,
    verify: VerifyOption = True,
    worktree: WorktreeOption = True,
    review: ReviewOption = True,
    usage: UsageOption = True,
    new: NewOption = False,
    name: NameOption = None,
    profile: ProfileOption = None,
    state: StateDirOption = None,
    config_dir: ConfigDirOption = None,
    connect: ConnectOption = None,
    remote: RemoteOption = None,
    token_file: TokenFileOption = None,
    ca: CaOption = None,
    cert: ClientCertOption = None,
    key: ClientKeyOption = None,
    local: LocalOption = False,
) -> None:
    """Start an interactive agent session.

    At startup, pick a saved session or start a new one (or start one with --new); ctrl+p
    picks again later. With --connect, or when an orbit serve is running (unless --local), join
    it instead, first picking one of the workspaces it serves, or adding the one here (ctrl+w
    picks again later), then a session, the picker offering to resume the one it has open.
    --remote NAME joins the one [remotes.NAME] in your config.toml names.
    A control plane serving mutual TLS is shown a client certificate, with --cert and --key,
    rather than the token.

    In the app: h/j/k/l move between windows; i jumps to the prompt, which talks to
    the planner, whose proposals wait on the task list as drafts, and ctrl+s stops the run;
    ctrl+o / ctrl+l open a picker for the planner's model / new tasks' tool access; enter
    on the task list edits queued tasks and ctrl+r runs them, each in a fresh agent
    that fetches it from orbit over MCP; verification steps run when it says it's done,
    and failures go back to it to fix.
    """
    user_config = resolve_config_dir(config_dir, os.environ)
    _refuse_console(user_config)
    given = _ClientFiles(token_file, ca, cert, key)
    flags = _Backend(model, tools, verify, worktree, review, usage, new, name)
    target = _target(connect, remote, user_config, given)
    state = resolve_state_dir(state, os.environ)
    run = TelemetryRun("orbit-chat", user_config, state, os.environ)
    if target is not None:
        _refuse_local(local, target.flag)
        _refuse_state_dir(ctx, target.flag)
        joining = _joining(ctx, target.files, user_config, _opening(flags))
        where = f"not orbit chat {target.flag}"
        repository = _repository_for(target.url, workspace)
        _join(replace(joining, repository=repository), target.url, where, run)
        return
    root = _workspace(workspace)
    found = None if local else _discovered(state, given, user_config)
    if found is not None:
        joining = _joining(ctx, found.files, user_config, _opening(flags))
        joining = replace(joining, repository=str(root))
        where = f"which runs at {found.url}; or pass --local to run one here"
        _join(joining, found.url, where, run)
        return
    _refuse_client_files(
        given, "--local runs one here" if local else "no orbit serve was found here"
    )
    # The only place the backend and frontend meet, and where the backend's external
    # adapters are plugged into its ports.
    profile = profile.expanduser().resolve() if profile else None
    directories = _Directories(root, state, user_config)
    picking = not flags.fresh
    # The TUI captures the warnings logged while it runs; those as telemetry stops go to stderr.
    with _telemetry(run) as telemetry:
        collector = _agent_collector(run)
        flags = replace(flags, telemetry=telemetry, gauges=telemetry.gauges, collector=collector)
        with _in_process(flags, directories, pick_session=picking, make=_backend) as backend:
            session = ThreadedSession(backend.build, _thread_context(profile))
            opening = Opening(pick=picking, repository=str(root))
            _launch(OrbitApp(session, opening=opening, telemetry=telemetry), profile)


@dataclass(frozen=True)
class _ClientFiles:
    """The files a chat joining a control plane shows it: the token in ``token_file``, or the
    client certificate ``cert`` with its ``key``, verifying it by the CA in ``ca``; each may be
    ``None``."""

    token_file: Path | None = None
    ca: Path | None = None
    cert: Path | None = None
    key: Path | None = None

    def over(self, configured: _ClientFiles) -> _ClientFiles:
        """These files over the ``configured`` ones: the token or client certificate given, if
        any, in place of the configured one, and the CA given, if any."""
        return replace(self if self.identified else configured, ca=self.ca or configured.ca)

    @property
    def identified(self) -> bool:
        """Whether these files name a token or a client certificate to show."""
        return (self.token_file, self.cert, self.key) != (None, None, None)


@dataclass(frozen=True)
class _Target:
    """The control plane at ``url`` a chat was asked to join, showing it ``files``; ``flag``
    is how it was asked, --connect or --remote."""

    url: str
    files: _ClientFiles
    flag: str


def _target(
    connect: str | None, remote: str | None, config_dir: Path, given: _ClientFiles
) -> _Target | None:
    """The control plane --connect, ``connect``, or --remote, ``remote``, names, as the user
    config in ``config_dir`` says, with the ``given`` files over the remote's; ``None`` if
    neither was given.

    Exits with status 2 if both were given, or the remote isn't configured.
    """
    if remote is None:
        return None if connect is None else _Target(_connect_url(connect), given, "--connect")
    if connect is not None:
        typer.echo(
            "--remote names the control plane to connect to, so it can't go with --connect.",
            err=True,
        )
        raise typer.Exit(2)
    configured = _remote(config_dir, remote)
    return _Target(configured.url, given.over(_remote_files(configured)), "--remote")


def on_this_machine(url: str) -> bool:
    """Whether the control plane at ``url`` runs on this machine, as its host is loopback, so
    a path here names the same directory there."""
    return is_loopback(urlsplit(url).hostname or "")


def _repository_for(url: str, workspace: Path) -> str | None:
    """The repository at ``workspace`` for the workspace picker of the control plane at ``url``
    to offer; ``None`` unless it runs on this machine, where the path names the same one."""
    return str(_workspace(workspace)) if on_this_machine(url) else None


def _connect_url(url: str) -> str:
    """The URL --connect gives, ``url``.

    Exits with status 2 if it isn't a ws:// or wss:// URL.
    """
    if not url.startswith(("ws://", "wss://")):
        typer.echo(f"orbit chat --connect takes a wss:// or ws:// URL, not {url}.", err=True)
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


def _remote_files(remote: RemoteSettings) -> _ClientFiles:
    """The files ``remote`` names."""
    paths = (remote.token_file, remote.ca, remote.cert, remote.key)
    return _ClientFiles(*(None if path is None else Path(path) for path in paths))


@dataclass(frozen=True)
class _Joining:
    """How a chat joins a running control plane: with the flags in ``ctx``, showing the token
    in ``token_file``, or without one the client certificate at ``cert`` with its key at
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


def _certified(
    joining: _Joining, cert: Path | None, key: Path | None, token_file: Path | None
) -> _Joining:
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


def _joining(
    ctx: typer.Context, files: _ClientFiles, config_dir: Path, opening: NewSession | None
) -> _Joining:
    """How a chat with the flags in ``ctx`` joins a control plane showing it ``files``, or the
    token in the config directory ``config_dir`` without one, and sending ``opening``, if any.

    Exits with status 2 if a client certificate was given with a token file.
    """
    token_path = files.token_file or TokenFile(config_dir).path
    joining = _Joining(ctx, token_path, files.ca, opening)
    return _certified(joining, files.cert, files.key, files.token_file)


def _opening(flags: _Backend) -> NewSession | None:
    """The new session the backend ``flags`` ask a joining chat to open, if any."""
    return NewSession(name=flags.name) if flags.fresh else None


def _refuse_client_files(given: _ClientFiles, reason: str) -> None:
    """Exit with status 2 if --token-file, --ca, --cert or --key were ``given`` to a chat
    running the control plane here, which would ignore them, ``reason`` saying why it runs
    here."""
    named = [flag for flag, path in _CLIENT_FLAGS.items() if getattr(given, path) is not None]
    if named:
        kind = "connection files" if "--token-file" in named else "TLS files"
        typer.echo(
            f"The {kind} given ({', '.join(named)}) say how to join a control plane, and"
            f" {reason}: pass --connect or --remote with them, or leave them out.",
            err=True,
        )
        raise typer.Exit(2)


_CLIENT_FLAGS = {"--token-file": "token_file", "--ca": "ca", "--cert": "cert", "--key": "key"}
"""The flags naming the files a joining chat shows or trusts, and the fields of
:class:`_ClientFiles` they set."""


def _refuse_state_dir(ctx: typer.Context, flag: str) -> None:
    """Exit with status 2 if --state-dir was given to a chat joining the control plane
    ``flag``, --connect or --remote, names, which keeps its own state and would ignore it."""
    if _given(ctx, "state"):
        typer.echo(
            f"--state-dir says where a control plane run here keeps its state, so it can't go"
            f" with {flag}.",
            err=True,
        )
        raise typer.Exit(2)


def _refuse_local(local: bool, flag: str) -> None:
    """Exit with status 2 if ``local`` was asked for along with ``flag``, --connect or
    --remote."""
    if local:
        typer.echo(f"--local runs the control plane here, so it can't go with {flag}.", err=True)
        raise typer.Exit(2)


def _join(joining: _Joining, url: str, where: str, run: TelemetryRun) -> None:
    """Run the TUI over the control plane at ``url``, as ``joining`` says, tracing its actions
    with the telemetry the user config here asks for, for orbit running as ``run`` says, then
    flush and stop it.

    Exits with status 2 if a flag that configures the control plane was given, saying it
    belongs on orbit serve, ``where`` saying which, or the telemetry configured can't start.
    """
    _refuse_serve_flags(joining.ctx, where)
    # The frontend user's own: the control plane's collector may be out of its reach.
    with _telemetry(run) as telemetry:
        _launch(_connected_app(url, joining, telemetry), None)


class LocalCertMissing(Exception):
    """A control plane on this machine serves mutual TLS, and the user config names no client
    certificate to show it; the message names the keys to set."""


@dataclass(frozen=True)
class Discovered:
    """A control plane found running here: the ``url`` a frontend here connects to, showing it
    ``files``."""

    url: str
    files: _ClientFiles


def discover_control_plane(state: Path, given: _ClientFiles, config_dir: Path) -> Discovered | None:
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


def _local_files(server: RunningServer, given: _ClientFiles, config_dir: Path) -> _ClientFiles:
    """The files a frontend on this machine shows ``server``: those ``given`` over the token in
    the config directory ``config_dir``, verifying it by the CA its run file names over TLS, or
    under mutual TLS the client certificate in the user config there instead of the token.

    Raises :class:`ConfigError` if the user config is invalid, and :class:`LocalCertMissing`
    if it names no client certificate under mutual TLS, and none is given.
    """
    ca = None if server.tls is None else Path(server.tls.ca)
    if given.identified:
        return given.over(_ClientFiles(ca=ca))
    if server.tls is None or not server.tls.mutual:
        return given.over(_ClientFiles(TokenFile(config_dir).path, ca))
    return given.over(replace(_local_cert(server, config_dir), ca=ca))


def _local_cert(server: RunningServer, config_dir: Path) -> _ClientFiles:
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
    return _ClientFiles(cert=Path(cert), key=Path(key))


def _discovered(state: Path, given: _ClientFiles, config_dir: Path) -> Discovered | None:
    """What :func:`discover_control_plane` finds, shown the files ``given``; a control plane
    found is never bypassed.

    Exits with status 2 if the token, the user config, the CA or the client certificate can't
    be used, the control plane turns the token away, or refuses the handshake.
    """
    token_file = given.token_file or TokenFile(config_dir).path
    refused = messages.LOCAL_TOKEN_REFUSED.format(path=token_file)
    with (
        _exit_on(TokenRejected, message=lambda _: refused),
        _exit_on(HandshakeRefused, message=_offering_local),
        _exit_on(*_LOCAL_ERRORS),
    ):
        return _configured(lambda: discover_control_plane(state, given, config_dir))


def _offering_local(refusal: HandshakeRefused) -> str:
    """What says the control plane found here refused the handshake, as ``refusal`` says,
    offering --local instead."""
    return f"{str(refusal).removesuffix('.')}; or pass --local to run one here."


_LOCAL_ERRORS = (
    TokenUnreadable,
    LocalCertMissing,
    CaUnreadable,
    ClientCertUnusable,
)
"""What stops a chat finding a control plane on this machine, said as it is."""


def _refuse_serve_flags(ctx: typer.Context, where: str) -> None:
    """Exit with status 2 if any of the flags that configure the control plane were given,
    saying they belong on orbit serve, ``where`` saying which."""
    given = [f"--{name}" for name in _SERVE_FLAGS if _given(ctx, name)]
    if given:
        typer.echo(
            f"{', '.join(given)} configure the control plane, so they belong on orbit serve,"
            f" {where}.",
            err=True,
        )
        raise typer.Exit(2)


def _given(ctx: typer.Context, name: str) -> bool:
    """Whether the option ``name`` was given, not left at its default."""
    source = ctx.get_parameter_source(name)  # Typer's own click, so its enum is compared by name.
    return source is not None and source.name != "DEFAULT"


def _connected_app(url: str, joining: _Joining, telemetry: Telemetry) -> OrbitApp:
    """The TUI over the control plane at ``url``, joined as ``joining`` says, tracing its
    actions with ``telemetry``.

    Exits with status 2 if ``url`` is a ws:// one beyond loopback, or the token, the CA or the
    client certificate can't be read.
    """
    with _exit_on(TokenUnreadable, CaUnreadable, ClientCertUnusable, PlaintextRefused):
        if joining.token_file is not None:
            read_token(joining.token_file)  # The session reads it again for each connection.
        context = _ssl_context(url, joining)
        client = WebSocketSession(url, joining.token_file, ssl_context=context)
    opening = Opening(joining.opening, joining.opening is None, joining.repository)
    return OrbitApp(client, client, opening, telemetry=telemetry)


def _ssl_context(url: str, joining: _Joining) -> ssl.SSLContext | None:
    """What verifies the control plane at ``url``, trusting the CA ``joining`` gives if any,
    and shows the client certificate it gives, if any: none for a ``ws://`` URL, which has no
    TLS.

    Raises :class:`CaUnreadable` if the CA can't be read, and :class:`ClientCertUnusable` if
    the client certificate can't be used.
    """
    if not url.startswith("wss://"):
        return None
    return client_ssl_context(joining.ca, joining.cert, joining.key)


def _thread_context(profile: Path | None) -> Callable[[], AbstractContextManager[None]] | None:
    """What the backend's thread runs inside: profiled into ``profile``, if there is one."""
    if profile is None:
        return None
    return lambda: profiled(profile, "backend")


def _launch(tui: OrbitApp, profile: Path | None) -> None:
    """Run ``tui``, profiled into the ``profile`` directory if there is one, capturing the
    warnings that would draw over it, then print them, and why its session is halted, if it
    is, as the halt's modal no longer shows it."""
    try:
        with tui.warnings.capturing():
            _run(tui, profile)
    finally:
        for line in tui.warnings.lines():
            typer.echo(line, err=True)
    if tui.storage_halt is not None:
        typer.echo(tui.storage_halt, err=True)


def _run(tui: OrbitApp, profile: Path | None) -> None:
    """Run ``tui``, profiled into the ``profile`` directory if there is one."""
    if profile is None:
        tui.run()
    else:
        asyncio.run(_run_profiled(tui, profile))


async def _run_profiled(tui: OrbitApp, directory: Path) -> None:
    """Run ``tui``, profiling its thread into ``directory``."""
    with profiled(directory, "ui"):
        await tui.run_async()
