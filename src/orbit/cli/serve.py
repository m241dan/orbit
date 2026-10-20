"""orbit serve: the control plane on its own, for frontends to connect to."""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import typer

from orbit.agent import (
    USER_CONFIG_FILE,
    AgentSession,
    ConfigError,
    ControlPlaneServer,
    ListenFailed,
    RunFile,
    ServerSettings,
    SqliteStore,
    TlsError,
    TokenError,
    TokenFile,
    WorkspaceLock,
    WorkspaceOwner,
    read_server,
    resolve_config_dir,
    resolve_state_dir,
    tls_warnings,
)
from orbit.cli.backend import INVALID_CONFIG, _Backend, _backend, _repository_lock, _workspace
from orbit.cli.errors import _exit_on
from orbit.cli.options import (
    CertOption,
    ClientCaOption,
    ConfigDirOption,
    HostOption,
    KeyOption,
    ModelOption,
    NameOption,
    NewOption,
    PortOption,
    ProfileOption,
    ReviewOption,
    StateDirOption,
    TlsOption,
    ToolsOption,
    UsageOption,
    VerifyOption,
    WorkspaceOption,
    WorktreeOption,
)
from orbit.profiling import profiled

logger = logging.getLogger(__name__)


def serve(
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
    host: HostOption = None,
    port: PortOption = None,
    tls: TlsOption = None,
    cert: CertOption = None,
    key: KeyOption = None,
    client_ca: ClientCaOption = None,
) -> None:
    """Run the control plane on its own, for frontends to connect to over a WebSocket.

    It opens the workspace's most recently active session (or a new one with --new or
    --name), and its work goes on with no frontend connected. Frontends show the token in
    the token file in your config directory. The first orbit on a repository owns it; while
    another does, this one is read-only, queueing the work it's asked to run. The first ctrl+c
    stops it, cancelling the work in progress; a second exits at once. Beyond loopback it
    serves over TLS alone, with --tls tls and a certificate and key.
    """
    profile = profile.expanduser().resolve() if profile else None
    state = resolve_state_dir(state, os.environ)
    user_config = resolve_config_dir(config_dir, os.environ)
    root = _workspace(workspace)
    os.chdir(root)
    given = {"host": host, "port": port, "tls": tls, "cert": cert, "key": key}
    settings = _server_settings(user_config, {**given, "client_ca": client_ca})
    token_file = TokenFile(user_config)
    _token(token_file)
    with _repository_lock(root) as (lock, owner):
        backend = _Backend(model, tools, verify, worktree, review, usage, new, name, owner, lock)
        build, store = _backend(backend, root, state, user_config, pick_session=False)
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        parts = _ServeParts(
            build,
            not backend.fresh,
            settings,
            _current_token(token_file),
            None if owner else RunFile(state, store.workspace_id),
            _banner(root, token_file, owner),
            lock,
        )
        _serve_until_stopped(parts, profile, user_config, store)
    typer.echo("orbit serve stopped.")


def _banner(
    root: Path, token_file: TokenFile, owner: WorkspaceOwner | None
) -> Callable[[str], str]:
    """What gives the line ``orbit serve`` prints once listening at a URL, serving the
    repository at ``root`` as its owner, or read-only while another orbit, ``owner``, owns it,
    saying where ``token_file`` is."""
    standing = "as its owner" if owner is None else f"read-only, as {owner.described} owns it"

    def banner(url: str) -> str:
        return f"Serving {root} at {url} {standing}; frontends show the token in {token_file.path}."

    return banner


def _serve_until_stopped(
    parts: "_ServeParts", profile: Path | None, user_config: Path, store: SqliteStore
) -> None:
    """Serve as ``parts`` say, profiled into ``profile`` if given, until stopped, then close
    ``store``.

    Exits with status 2 if the server can't listen, naming the user config in
    ``user_config`` that sets its port.
    """
    try:
        asyncio.run(_served(parts, profile))
    except ListenFailed as failed:
        config_file = user_config / USER_CONFIG_FILE
        typer.echo(
            f"Can't listen on port {failed.port}: {failed.reason}. Choose another with --port,"
            f" or with port in [server] in {config_file}.",
            err=True,
        )
        raise typer.Exit(2) from None
    finally:
        asyncio.run(store.close())


def _server_settings(config_dir: Path, given: Mapping[str, object]) -> ServerSettings:
    """The server settings in the user config in ``config_dir``, with the ``given`` ones that
    aren't ``None`` over them, after warning of what they serve with.

    Exits with status 2 if the file is invalid, or the server can't serve as they say.
    """
    with _exit_on(ConfigError, prefix=INVALID_CONFIG):
        settings = read_server(config_dir)
    update = {k: str(v) if isinstance(v, Path) else v for k, v in given.items() if v is not None}
    settings = settings.model_copy(update=update)
    with _exit_on(TlsError):
        for warning in tls_warnings(settings):
            typer.echo(f"Warning: {warning}", err=True)
    return settings


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


@dataclass(frozen=True)
class _ServeParts:
    """What ``orbit serve`` serves: the session ``build`` makes, with the most recently active
    saved one opened if ``latest``, on the server ``settings`` say, to frontends showing the
    token ``token`` gives as they connect, saying where it is in ``run_file``, if it owns the
    repository, while it does, the ``banner`` it prints once listening, given the server's
    URL, and the workspace ``lock``, which names the server's URL while it's held."""

    build: Callable[[], AgentSession]
    latest: bool
    settings: ServerSettings
    token: Callable[[], str]
    run_file: RunFile | None
    banner: Callable[[str], str]
    lock: WorkspaceLock


async def _served(parts: _ServeParts, profile: Path | None) -> None:
    """Serve as ``parts`` say, profiled into ``profile`` if given, until the first ctrl+c or
    SIGTERM, then stop.

    Raises :class:`ListenFailed` if the server can't listen.
    """
    with profiled(profile, "serve") if profile else nullcontext():
        session = parts.build()
        server = ControlPlaneServer(session, parts.token, parts.settings, typer.echo)
        try:
            if parts.latest:
                await session.open_latest()
            url = await server.start()
        except BaseException:
            await session.close()
            raise
        await _until_stopped(session, server, parts, url)


async def _until_stopped(
    session: AgentSession, server: ControlPlaneServer, parts: _ServeParts, url: str
) -> None:
    """Serve ``session`` on ``server``, listening at ``url``, until the first ctrl+c or
    SIGTERM, then stop it, removing the run file it wrote.

    Exits with status 2 if the run file can't be written.
    """
    stopping = asyncio.Event()
    _on_signals(stopping)
    written: RunFile | None = None
    try:
        if parts.run_file is not None:
            _write_run_file(parts.run_file, url)
            written = parts.run_file
        parts.lock.announce(url)
        typer.echo(parts.banner(url))
        await stopping.wait()
    finally:
        try:
            await server.stop(session.close)
        finally:
            # One that couldn't be written may not be removable either.
            if written is not None:
                written.remove()


def _write_run_file(run_file: RunFile, url: str) -> None:
    """Say in ``run_file`` that this process is serving at ``url``.

    Exits with status 2 if it can't.
    """
    try:
        run_file.write(url, os.getpid())
    except OSError as exc:
        typer.echo(f"Can't write the run file {run_file.path}: {exc}", err=True)
        raise typer.Exit(2) from None


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
