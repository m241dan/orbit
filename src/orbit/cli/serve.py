"""orbit serve: the control plane on its own, for frontends to connect to."""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import typer

from orbit.agent import (
    USER_CONFIG_FILE,
    ConfigError,
    ControlPlane,
    ControlPlaneServer,
    ListenFailed,
    LocalDiscoveryUnavailable,
    LocalTls,
    RunFile,
    ServerSettings,
    TlsError,
    TokenError,
    TokenFile,
    WorkspaceLock,
    local_tls,
    read_server,
    resolve_config_dir,
    resolve_state_dir,
    running_server,
    tls_warnings,
)
from orbit.cli.backend import _Backend, _ServedWorkspace, _serving, _workspace, _Workspaces
from orbit.cli.errors import INVALID_CONFIG, _configured, _exit_on
from orbit.cli.options import (
    AddOption,
    CertOption,
    ClientCaOption,
    ConfigDirOption,
    HostOption,
    KeyOption,
    ModelOption,
    PortOption,
    ProfileOption,
    ReviewOption,
    StateDirOption,
    TlsOption,
    ToolsOption,
    UsageOption,
    VerifyOption,
    WorktreeOption,
)
from orbit.profiling import profiled

logger = logging.getLogger(__name__)


def serve(
    add: AddOption = None,
    model: ModelOption = None,
    tools: ToolsOption = None,
    verify: VerifyOption = True,
    worktree: WorktreeOption = True,
    review: ReviewOption = True,
    usage: UsageOption = True,
    profile: ProfileOption = None,
    state: StateDirOption = None,
    config_dir: ConfigDirOption = None,
    host: HostOption = None,
    port: PortOption = None,
    tls: TlsOption = None,
    cert: CertOption = None,
    key: KeyOption = None,
    client_ca: ClientCaOption = None,
) -> None:
    """Run the control plane on its own, for frontends to connect to over a WebSocket.

    It serves every workspace added with --add, now or by an earlier orbit serve, or from a
    frontend, wherever it's started, each opening its most recently active session, and their
    work goes on with no frontend connected; the backend flags apply to every workspace it
    serves. One runs per state directory. Frontends show the token in the token file in your
    config directory. The first orbit on a repository owns it; while another does, the
    workspace is read-only here, queueing the work it's asked to run. The first ctrl+c stops
    it, cancelling the work in progress; a second exits at once. Beyond loopback it serves over
    TLS alone, with --tls tls and a certificate and key; with --tls mtls and --client-ca,
    frontends show a client certificate that CA signed rather than the token.
    """
    # #lizard forgives(parameter_count): Typer takes each of the command's options as a parameter.
    profile = profile.expanduser().resolve() if profile else None
    state = resolve_state_dir(state, os.environ)
    user_config = resolve_config_dir(config_dir, os.environ)
    _refuse_second(state)
    added = [_workspace(path) for path in add or ()]
    cert, key, client_ca = (_absolute(path) for path in (cert, key, client_ca))
    given = {"host": host, "port": port, "tls": tls, "cert": cert, "key": key}
    settings = _server_settings(user_config, {**given, "client_ca": client_ca})
    token_file = TokenFile(user_config)
    _token(token_file)
    flags = _Backend(model, tools, verify, worktree, review, usage, new=False, name=None)
    with _serving(flags, added, state, user_config) as serving:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        parts = _ServeParts(
            serving.build,
            settings,
            _current_token(token_file),
            _current_revoked(user_config, settings.revoked),
            RunFile(state),
            _local_tls(settings, Path.cwd()),
            _banner(serving.workspaces, token_file, settings),
            serving.served,
        )
        _serve_until_stopped(parts, profile, user_config)
    typer.echo("orbit serve stopped.")


def _refuse_second(state: Path) -> None:
    """Exit with status 2 if an ``orbit serve`` is already running on the ``state``
    directory, naming it."""
    running = running_server(state)
    if running is None:
        return
    typer.echo(
        f"orbit serve is already running at {running.url} (pid {running.pid}); use --state-dir"
        " for a separate one.",
        err=True,
    )
    raise typer.Exit(2)


def _banner(
    workspaces: Sequence[_ServedWorkspace], token_file: TokenFile, settings: ServerSettings
) -> Callable[[str], str]:
    """What gives the line ``orbit serve`` prints once listening at a URL, listing the
    ``workspaces`` it serves, each as its owner, or read-only while another orbit owns it, or
    saying it serves none yet, and what frontends show it: where ``token_file`` is, or under
    mutual TLS the client CA in ``settings``."""
    if settings.tls == "mtls":
        shown = f"a client certificate {settings.client_ca} signed"
    else:
        shown = f"the token in {token_file.path}"
    listed = "; ".join(_standing(workspace) for workspace in workspaces)
    served = listed or "no workspaces yet; add one with orbit serve --add PATH"

    def banner(url: str) -> str:
        return f"Serving at {url}: {served}. Frontends show {shown}."

    return banner


def _standing(workspace: _ServedWorkspace) -> str:
    """How ``orbit serve`` serves ``workspace``: as its owner, or read-only, naming the owner."""
    owner = workspace.owner
    if owner is None:
        return f"{workspace.root} as its owner"
    return f"{workspace.root} read-only, as {owner.described} owns it"


def _serve_until_stopped(parts: "_ServeParts", profile: Path | None, user_config: Path) -> None:
    """Serve as ``parts`` say, profiled into ``profile`` if given, until stopped.

    Exits with status 2 if the server can't listen, naming the user config in
    ``user_config`` that sets its port.
    """
    config_file = user_config / USER_CONFIG_FILE

    def unable(failed: ListenFailed) -> str:
        return (
            f"Can't listen on port {failed.port}: {failed.reason}. Choose another with --port,"
            f" or with port in [server] in {config_file}."
        )

    with _exit_on(ListenFailed, message=unable):
        asyncio.run(_served(parts, profile))


def _absolute(path: Path | None) -> Path | None:
    """``path`` with ``~`` expanded, taken from the directory ``orbit serve`` started in if
    relative; ``None`` if not given."""
    return path.expanduser().resolve() if path else None


def _server_settings(config_dir: Path, given: Mapping[str, object]) -> ServerSettings:
    """The server settings in the user config in ``config_dir``, with the ``given`` ones that
    aren't ``None`` over them, after warning of what they serve with.

    Exits with status 2 if the file is invalid, or the server can't serve as they say.
    """
    settings = _configured(lambda: read_server(config_dir))
    update = {k: str(v) if isinstance(v, Path) else v for k, v in given.items() if v is not None}
    settings = settings.model_copy(update=update)
    with _exit_on(TlsError):
        for warning in tls_warnings(settings):
            typer.echo(f"Warning: {warning}", err=True)
    return settings


def _local_tls(settings: ServerSettings, root: Path) -> LocalTls | None:
    """How a frontend on this machine reaches the server ``settings`` serve with TLS, relative
    paths taken from ``root``, the directory it started in; ``None`` for plaintext, or, with a
    warning saying why, when it can't find it on its own."""
    if settings.tls == "off":
        return None
    try:
        return local_tls(settings, root)
    except LocalDiscoveryUnavailable as exc:
        typer.echo(f"Warning: {exc}", err=True)
        return None


def _token(token_file: TokenFile) -> str:
    """The token in ``token_file``, made if there's none.

    Exits with status 2 if it can't be used.
    """
    with _exit_on(TokenError):
        return token_file.token()


def _current_token(token_file: TokenFile) -> Callable[[], str]:
    """What gives the token in ``token_file`` as it is now, empty with a warning if it can't be
    used, so that it admits no one."""

    def current() -> str:
        try:
            return token_file.token()
        except TokenError as exc:
            logger.warning("%s", exc)
            return ""

    return current


def _current_revoked(config_dir: Path, first: Collection[str]) -> Callable[[], Collection[str]]:
    """What gives the revoked fingerprints in the user config in ``config_dir`` as it is now,
    ``first`` those it started with; while it's invalid, a warning and those last read, so a
    fingerprint revoked stays revoked."""
    last = [first]

    def current() -> Collection[str]:
        try:
            last[0] = read_server(config_dir).revoked
        except ConfigError as exc:
            logger.warning("%s%s", INVALID_CONFIG, exc)
        return last[0]

    return current


@dataclass(frozen=True)
class _ServeParts:
    """What ``orbit serve`` serves: the control plane ``build`` makes, each workspace's most
    recently active saved session opened, on the server ``settings`` say, to frontends showing
    the token ``token`` gives as they connect, or under mutual TLS a certificate not among those
    ``revoked`` gives then, saying where it is in ``run_file`` while it listens, and how
    frontends here reach it over TLS, ``local``, the ``banner`` it prints once listening, given
    the server's URL, and the workspaces ``served``, whose locks name the server's URL while
    they're held."""

    build: Callable[[], ControlPlane]
    settings: ServerSettings
    token: Callable[[], str]
    revoked: Callable[[], Collection[str]]
    run_file: RunFile
    local: LocalTls | None
    banner: Callable[[str], str]
    served: _Workspaces


async def _served(parts: _ServeParts, profile: Path | None) -> None:
    """Serve as ``parts`` say, profiled into ``profile`` if given, until the first ctrl+c or
    SIGTERM, then stop.

    Raises :class:`ListenFailed` if the server can't listen.
    """
    with profiled(profile, "serve") if profile else nullcontext():
        plane = parts.build()
        server = ControlPlaneServer(plane, parts.token, parts.settings, typer.echo)
        server.revoked = parts.revoked
        try:
            for host in plane.hosts:
                await host.open_latest()
            url = await server.start()
        except BaseException:
            await plane.close()
            raise
        await _until_stopped(plane, server, parts, url)


async def _until_stopped(
    plane: ControlPlane, server: ControlPlaneServer, parts: _ServeParts, url: str
) -> None:
    """Serve ``plane`` on ``server``, listening at ``url``, until the first ctrl+c or
    SIGTERM, then stop it, removing the run file it wrote.

    Exits with status 2 if the run file or a workspace lock can't be written.
    """
    stopping = asyncio.Event()
    _on_signals(stopping)
    written = False
    try:
        _write_run_file(parts.run_file, url, parts.local)
        written = True
        for lock in parts.served.locks:
            _announce(lock, url)
        parts.served.url = url
        typer.echo(parts.banner(url))
        await stopping.wait()
    finally:
        try:
            await server.stop(plane.close)
        finally:
            if written:  # One that couldn't be written may not be removable either.
                parts.run_file.remove()


def _write_run_file(run_file: RunFile, url: str, local: LocalTls | None) -> None:
    """Say in ``run_file`` that this process is serving at ``url``, reached from this machine
    as ``local`` says, in place of a stale one.

    Exits with status 2 if it can't.
    """
    with _exit_on(OSError, message=lambda exc: f"Can't write the run file {run_file.path}: {exc}"):
        run_file.write(url, os.getpid(), local)


def _announce(lock: WorkspaceLock, url: str) -> None:
    """Say in ``lock`` that its owner is serving at ``url``.

    Exits with status 2 if it can't.
    """
    with _exit_on(
        OSError, message=lambda exc: f"Can't write the workspace lock {lock.path}: {exc}"
    ):
        lock.announce(url)


def _on_signals(stopping: asyncio.Event) -> None:
    """Have the first ctrl+c or SIGTERM set ``stopping``, and a second ctrl+c exit at once."""
    loop = asyncio.get_running_loop()

    def first() -> None:
        stopping.set()
        loop.add_signal_handler(signal.SIGINT, _exit_now)

    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, first)


def _exit_now() -> None:
    """Exit at once, as a second ctrl+c asks, without stopping anything."""
    os._exit(130)
