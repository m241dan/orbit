"""orbit serve: the control plane on its own, for frontends to connect to."""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Collection, Mapping
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
    WorkspaceOwner,
    has_git_identity,
    in_git_work_tree,
    local_tls,
    read_server,
    resolve_config_dir,
    resolve_state_dir,
    tls_warnings,
)
from orbit.cli.backend import _Backend, _backend, _Directories, _in_process, _workspace
from orbit.cli.errors import INVALID_CONFIG, _configured, _exit_on
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
    serves over TLS alone, with --tls tls and a certificate and key; with --tls mtls and
    --client-ca, frontends show a client certificate that CA signed rather than the token.
    """
    profile = profile.expanduser().resolve() if profile else None
    state = resolve_state_dir(state, os.environ)
    user_config = resolve_config_dir(config_dir, os.environ)
    root = _workspace(workspace)
    if worktree:
        _require_git_identity(root)
    os.chdir(root)
    given = {"host": host, "port": port, "tls": tls, "cert": cert, "key": key}
    settings = _server_settings(user_config, {**given, "client_ca": client_ca})
    token_file = TokenFile(user_config)
    _token(token_file)
    flags = _Backend(model, tools, verify, worktree, review, usage, new, name)
    directories = _Directories(root, state, user_config)
    with _in_process(flags, directories, pick_session=False, make=_backend) as backend:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        parts = _ServeParts(
            backend.build,
            not flags.fresh,
            settings,
            _current_token(token_file),
            _current_revoked(user_config, settings.revoked),
            RunFile(state, backend.store.workspace_id),
            _local_tls(settings, root),
            _banner(root, token_file, backend.owner, settings),
            backend.lock,
        )
        _serve_until_stopped(parts, profile, user_config)
    typer.echo("orbit serve stopped.")


def _require_git_identity(root: Path) -> None:
    """Exit with status 2 if the workspace at ``root`` is a git repository that git has no
    identity for commits in, as the coder's worktree commits there."""
    if not in_git_work_tree(str(root)) or has_git_identity(str(root)):
        return
    typer.echo(
        f"Git has no identity for commits in {root}: set user.name and user.email"
        ' (git config --global user.name "…"), or mount your ~/.gitconfig into the container.',
        err=True,
    )
    raise typer.Exit(2)


def _banner(
    root: Path, token_file: TokenFile, owner: WorkspaceOwner | None, settings: ServerSettings
) -> Callable[[str], str]:
    """What gives the line ``orbit serve`` prints once listening at a URL, serving the
    repository at ``root`` as its owner, or read-only while another orbit, ``owner``, owns it,
    saying what frontends show it: where ``token_file`` is, or under mutual TLS the client CA
    in ``settings``."""
    standing = "as its owner" if owner is None else f"read-only, as {owner.described} owns it"
    if settings.tls == "mtls":
        shown = f"a client certificate {settings.client_ca} signed"
    else:
        shown = f"the token in {token_file.path}"

    def banner(url: str) -> str:
        return f"Serving {root} at {url} {standing}; frontends show {shown}."

    return banner


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
    paths taken from ``root``; ``None`` for plaintext, or, with a warning saying why, when it
    can't find it on its own."""
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
    """What ``orbit serve`` serves: the control plane ``build`` makes, with its workspace's most
    recently active saved session opened if ``latest``, on the server ``settings`` say, to
    frontends showing the token ``token`` gives as they connect, or under mutual TLS a
    certificate not among those ``revoked`` gives then, saying where it is in ``run_file``
    while it owns the repository, and how frontends here reach it over TLS, ``local``, the
    ``banner`` it prints once listening, given the server's URL, and the workspace ``lock``,
    which names the server's URL while it's held."""

    build: Callable[[], ControlPlane]
    latest: bool
    settings: ServerSettings
    token: Callable[[], str]
    revoked: Callable[[], Collection[str]]
    run_file: RunFile
    local: LocalTls | None
    banner: Callable[[str], str]
    lock: WorkspaceLock


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
            if parts.latest:
                await plane.host.open_latest()
            url = await server.start()
        except BaseException:
            await plane.close()
            raise
        await _until_stopped(plane, server, parts, url)


async def _until_stopped(
    plane: ControlPlane, server: ControlPlaneServer, parts: _ServeParts, url: str
) -> None:
    """Serve ``plane`` on ``server``, listening at ``url``, until the first ctrl+c or
    SIGTERM, then stop it, removing the run file it wrote: at once if it owns the repository,
    else once it takes the repository over.

    Exits with status 2 if the workspace lock, or the run file at once, can't be written.
    """
    stopping = asyncio.Event()
    _on_signals(stopping)
    written: list[RunFile] = []
    plane.host.owned = _taking_over(parts.run_file, url, parts.local, written)
    try:
        if not plane.read_only:
            _write_run_file(parts.run_file, url, parts.local)
            written.append(parts.run_file)
        _announce(parts.lock, url)
        typer.echo(parts.banner(url))
        await stopping.wait()
    finally:
        try:
            await server.stop(plane.close)
        finally:
            # One that couldn't be written may not be removable either.
            for run_file in written:
                run_file.remove()


def _write_run_file(run_file: RunFile, url: str, local: LocalTls | None) -> None:
    """Say in ``run_file`` that this process is serving at ``url``, reached from this machine
    as ``local`` says.

    Exits with status 2 if it can't.
    """
    with _exit_on(OSError, message=lambda exc: _unwritable(run_file, exc)):
        run_file.write(url, os.getpid(), local)


def _taking_over(
    run_file: RunFile, url: str, local: LocalTls | None, written: list[RunFile]
) -> Callable[[], None]:
    """What says in ``run_file`` that this process is serving at ``url``, reached from this
    machine as ``local`` says, as it takes the repository over, adding it to ``written``; if it
    can't, it logs a warning saying why and serves on."""

    def owned() -> None:
        try:
            run_file.write(url, os.getpid(), local)
        except OSError as exc:
            logger.warning("%s", _unwritable(run_file, exc))
            return
        written.append(run_file)

    return owned


def _unwritable(run_file: RunFile, exc: OSError) -> str:
    """What says ``run_file`` can't be written, as ``exc`` says."""
    return f"Can't write the run file {run_file.path}: {exc}"


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
