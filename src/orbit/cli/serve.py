"""orbit serve: the control plane on its own, for frontends to connect to."""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

import typer

from orbit.agent import (
    INVALID_CONFIG,
    USER_CONFIG_FILE,
    Browsing,
    ConfigError,
    ControlPlane,
    ControlPlaneServer,
    DirectoryLister,
    ListenFailed,
    LocalDiscoveryUnavailable,
    LocalTls,
    RunFile,
    ServedWorkspace,
    ServedWorkspaces,
    ServeLock,
    ServerSettings,
    StoreFailed,
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
from orbit.cli.backend import _Backend, _workspace
from orbit.cli.errors import _configured, _exit_on
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
from orbit.cli.served import Added, _serving
from orbit.cli.telemetry import _telemetry
from orbit.profiling import profiled
from orbit.protocol import looks_like_url
from orbit.telemetry import TelemetryRun

logger = logging.getLogger(__name__)
CANT_OPEN = "Orbit couldn't open a workspace's latest session: "


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
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    with _sole_serve(state):
        added = [_added(text) for text in add or ()]
        cert, key, client_ca = (_absolute(path) for path in (cert, key, client_ca))
        given = {"host": host, "port": port, "tls": tls, "cert": cert, "key": key}
        settings = _server_settings(user_config, {**given, "client_ca": client_ca})
        token_file = TokenFile(user_config)
        token = _frontend_token(token_file, settings)
        flags = _Backend(model, tools, verify, worktree, review, usage, new=False, name=None)
        clone_dir = _clone_dir(settings, state)
        with (
            _telemetry(TelemetryRun("orbit-serve", user_config, state, os.environ)) as telemetry,
            _serving(
                replace(flags, telemetry=telemetry), added, state, user_config, clone_dir
            ) as serving,
        ):
            parts = _ServeParts(
                lambda: serving.build(_browsing(settings)),
                settings,
                token,
                _current_revoked(user_config, settings.revoked),
                RunFile(state),
                _local_tls(settings, Path.cwd()),
                _banner(serving.workspaces, token_file, settings),
                serving.served,
            )
            _serve_until_stopped(parts, profile, user_config)
    typer.echo("orbit serve stopped.")


def _added(text: str) -> Added:
    """The workspace ``--add`` names with ``text``: a URL, to clone, or else the directory at
    its path.

    Exits with status 2 if it's a path that doesn't exist or isn't a directory.
    """
    return text if looks_like_url(text) else _workspace(Path(text))


def _browsing(settings: ServerSettings) -> Browsing:
    """How frontends browse this host: from the account's home, unless confined to the
    browsable root ``settings`` give."""
    return Browsing(DirectoryLister(), str(Path.home()), settings.browse_root)


def _clone_dir(settings: ServerSettings, state: Path) -> Path:
    """Where repositories added by URL are cloned: ``clone_dir`` in ``settings``, else
    ``workspaces`` in the ``state`` directory."""
    return Path(settings.clone_dir) if settings.clone_dir else state / "workspaces"


@contextmanager
def _sole_serve(state: Path) -> Iterator[None]:
    """Hold the serve lock on the ``state`` directory through the block, as the one ``orbit
    serve`` running on it.

    Exits with status 2 if another is running on it, or the lock can't be taken.
    """
    lock = ServeLock(state)
    with _exit_on(OSError, message=lambda exc: f"Can't take the serve lock {lock.path}: {exc}"):
        taken = lock.take()
    try:
        _refuse_second(state, taken)
        yield
    finally:
        lock.close()


def _refuse_second(state: Path, taken: bool) -> None:
    """Exit with status 2 if an ``orbit serve`` is already running on the ``state``
    directory, naming it: one its run file names, or, if this hasn't ``taken`` the serve lock,
    one that holds it, before its run file is written. A run file naming this process is stale,
    left by a serve that didn't stop cleanly whose pid this one now has, as pid 1 in a
    container always does."""
    running = running_server(state)
    if running is not None and running.pid != os.getpid():
        where = f"at {running.url} (pid {running.pid})"
    elif not taken:
        where = f"on {state}"
    else:
        return
    typer.echo(
        f"orbit serve is already running {where}; use --state-dir for a separate one.", err=True
    )
    raise typer.Exit(2)


def _banner(
    workspaces: Sequence[ServedWorkspace], token_file: TokenFile, settings: ServerSettings
) -> Callable[[str], str]:
    """What gives the line ``orbit serve`` prints once listening at a URL, listing the
    ``workspaces`` it serves, each as its owner, or read-only while another orbit owns it, or
    saying it serves none yet, and what frontends show it: where ``token_file`` is, or under
    mutual TLS the client CA in ``settings``."""
    if settings.mutual:
        shown = f"a client certificate {settings.client_ca} signed"
    else:
        shown = f"the token in {token_file.path}"
    listed = "; ".join(_standing(workspace) for workspace in workspaces)
    served = listed or "no workspaces yet; add one with orbit serve --add PATH"

    def banner(url: str) -> str:
        return f"Serving at {url}: {served}. Frontends show {shown}."

    return banner


def _standing(workspace: ServedWorkspace) -> str:
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

    Exits with status 2 if the file or a flag is invalid, or the server can't serve as they say.
    """
    read = _configured(lambda: read_server(config_dir))
    settings = _configured(lambda: read.with_flags(given))
    with _exit_on(TlsError):
        for warning in tls_warnings(settings):
            typer.echo(f"Warning: {warning}", err=True)
    return settings


def _local_tls(settings: ServerSettings, root: Path) -> LocalTls | None:
    """How a frontend on this machine reaches the server ``settings`` serve with TLS, relative
    paths taken from ``root``, the directory it started in; ``None`` for plaintext, or, with a
    warning saying why, when it can't find it on its own."""
    if not settings.secure:
        return None
    try:
        return local_tls(settings, root)
    except LocalDiscoveryUnavailable as exc:
        typer.echo(f"Warning: {exc}", err=True)
        return None


def _frontend_token(token_file: TokenFile, settings: ServerSettings) -> Callable[[], str]:
    """What gives the token frontends show the server ``settings`` serve, from ``token_file``,
    made if there's none; under mutual TLS, which ignores any token, an empty one, leaving the
    file alone.

    Exits with status 2 if the token file is needed and can't be used.
    """
    if not settings.uses_token:
        return _no_token
    _token(token_file)
    return _current_token(token_file)


def _no_token() -> str:
    """No token, for a server under mutual TLS, where none is asked for."""
    return ""


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
    recently active saved session opened and its background work started, on the server
    ``settings`` say, to frontends showing the token ``token`` gives as they connect, or under
    mutual TLS a certificate not among those ``revoked`` gives then, saying where it is in
    ``run_file`` while it listens, and how frontends here reach it over TLS, ``local``, the
    ``banner`` it prints once listening, given the server's URL, and the workspaces ``served``,
    whose locks name the server's URL while they're held."""

    build: Callable[[], ControlPlane]
    settings: ServerSettings
    token: Callable[[], str]
    revoked: Callable[[], Collection[str]]
    run_file: RunFile
    local: LocalTls | None
    banner: Callable[[str], str]
    served: ServedWorkspaces


async def _served(parts: _ServeParts, profile: Path | None) -> None:
    """Serve as ``parts`` say, profiled into ``profile`` if given, until the first ctrl+c or
    SIGTERM, then stop.

    Raises :class:`ListenFailed` if the server can't listen, and exits with status 2 if a store
    fails as a workspace's latest session opens.
    """
    with profiled(profile, "serve") if profile else nullcontext():
        plane = parts.build()
        plane.cap_coders(parts.settings.max_coders)
        server = ControlPlaneServer(plane, parts.token, parts.settings, typer.echo)
        server.revoked = parts.revoked
        try:
            with _exit_on(StoreFailed, message=lambda exc: f"{CANT_OPEN}{exc}"):
                await plane.start()
            url = await server.start()
        except BaseException:
            try:
                await server.stop(_nothing)
            finally:
                await plane.close()
            raise
        await _until_stopped(plane, server, parts, url)


async def _nothing() -> None:
    """Do nothing, for a server stopped before its control plane is closed."""


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
        # A workspace whose lock is taken from here on names the URL as it's added.
        parts.served.url = url
        for lock in parts.served.locks:
            _announce(lock, url)
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
