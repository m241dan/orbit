"""Command-line interface for orbit."""

import asyncio
import logging
import os
import secrets
import signal
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import (
    MODEL_ALIASES,
    USER_CONFIG_FILE,
    Agents,
    AgentSession,
    ClaudeCliRunner,
    ConfigError,
    ControlPlaneServer,
    FallbackFiles,
    GitCoderTree,
    ImportBlocked,
    ListenFailed,
    McpHost,
    OrbitConfig,
    RecoveryFailed,
    Role,
    RunFile,
    ServerSettings,
    SqliteStore,
    StorageSettings,
    StoreError,
    TokenError,
    TokenFile,
    UsageApi,
    Verification,
    git_top_level,
    import_session_files,
    imported_note,
    in_git_work_tree,
    is_loopback,
    legacy_dir,
    orbit_dir,
    read_config,
    read_server,
    read_storage,
    recover_parked,
    recovered_note,
    resolve_config_dir,
    resolve_state_dir,
    run_step,
    workspace_state,
    write_checks,
)
from orbit.client import TokenUnreadable, WebSocketSession, read_token
from orbit.profiling import profiled
from orbit.protocol import PLANNER, NewSession, Settings, ThreadedSession, ToolAccess
from orbit.tui import OrbitApp

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)
db = typer.Typer(help="Look after orbit's database of saved sessions.", no_args_is_help=True)
app.add_typer(db, name="db")


def _workspace(path: Path) -> Path:
    """The project directory at ``path``: its git work tree's top level, if it's in one.

    Exits with status 2 if ``path`` doesn't exist or isn't a directory.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        typer.echo(f"Workspace {resolved} doesn't exist.", err=True)
        raise typer.Exit(2)
    if not resolved.is_dir():
        typer.echo(f"Workspace {resolved} isn't a directory.", err=True)
        raise typer.Exit(2)
    top = git_top_level(str(resolved))
    return Path(top) if top else resolved


# Options declared apart, so chat stays short.
WorkspaceOption = Annotated[
    Path,
    typer.Option(
        "-C",
        "--workspace",
        help="The project to work on (default: the current directory). Inside a git"
        " repository, its top level.",
    ),
]
NewOption = Annotated[
    bool,
    typer.Option(
        "--new", help="Start a new session rather than picking one of the saved sessions."
    ),
]
NameOption = Annotated[
    str | None, typer.Option(help="Start a new session with this name (implies --new).")
]
ProfileOption = Annotated[
    Path | None,
    typer.Option(
        help="Write a profile of each thread, and a log of every time the UI or backend was"
        " blocked for over 50 ms, to this directory.",
    ),
]

StateDirOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="The directory orbit keeps its database of saved sessions, and their locks, in"
        " (default: $ORBIT_STATE_DIR, else $XDG_STATE_HOME/orbit, else ~/.local/state/orbit).",
    ),
]

ConfigDirOption = Annotated[
    Path | None,
    typer.Option(
        "--config-dir",
        help="The directory of your own config.toml, whose [storage] sets what orbit keeps of"
        " sessions (default: $ORBIT_CONFIG_DIR, else $XDG_CONFIG_HOME/orbit, else"
        " ~/.config/orbit).",
    ),
]


ModelOption = Annotated[
    str | None,
    typer.Option(
        help=f"The planner's model: an alias ({', '.join(MODEL_ALIASES)}) or full model name."
    ),
]
ToolsOption = Annotated[
    ToolAccess | None,
    typer.Option(
        help="Default tool access for new tasks: off (none), restricted (read-only; anything"
        " needing approval is refused; the default), full (auto-approve everything)."
    ),
]
VerifyOption = Annotated[
    bool,
    typer.Option(
        help="Run the verification steps in .orbit/config.toml once at startup, and after"
        " each task; a failure after a task goes back to the agent to fix."
    ),
]
WorktreeOption = Annotated[
    bool,
    typer.Option(
        help="Run tasks in a separate git worktree and merge their verified commits into"
        " the current branch (only inside a git repository).",
    ),
]
ReviewOption = Annotated[
    bool,
    typer.Option(
        help="Review each landed commit in the background and queue the reviewer's"
        " suggestions as tasks (needs the worktree).",
    ),
]
UsageOption = Annotated[
    bool,
    typer.Option(
        help="Show your Claude subscription's session and weekly usage limits, read with"
        " your Claude login's token from ~/.claude/.credentials.json.",
    ),
]


ConnectOption = Annotated[
    str | None,
    typer.Option(
        "--connect",
        metavar="URL",
        help="Connect to the control plane orbit serve runs at this ws:// URL rather than run"
        " one here; the backend flags belong on orbit serve then.",
    ),
]
TokenFileOption = Annotated[
    Path | None,
    typer.Option(
        "--token-file",
        help="The file holding the token to show the control plane --connect names (default:"
        " token in the config directory).",
    ),
]

# The chat flags that configure the control plane, which a connected chat doesn't run.
_SERVE_FLAGS = ("model", "tools", "verify", "worktree", "review", "usage", "profile")


@dataclass(frozen=True)
class _Backend:
    """What the backend flags ask for: the planner's ``model``, new tasks' ``tools``, and
    whether to ``verify``, run tasks in a ``worktree``, ``review`` landed work and show the
    ``usage`` limits; a ``new`` session, perhaps with a ``name``."""

    model: str | None
    tools: ToolAccess | None
    verify: bool
    worktree: bool
    review: bool
    usage: bool
    new: bool
    name: str | None

    @property
    def fresh(self) -> bool:
        """Whether a new session was asked for."""
        return self.new or self.name is not None


@app.command()
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
    token_file: TokenFileOption = None,
) -> None:
    """Start an interactive agent session.

    At startup, pick a saved session or start a new one (or start one with --new); ctrl+p
    switches later. With --connect, show the session a running orbit serve has open instead.

    In the app: h/j/k/l move between windows; i jumps to the prompt, which talks to
    the planner, whose proposals wait on the task list as drafts, and ctrl+s stops the run;
    ctrl+o / ctrl+l open a picker for the planner's model / new tasks' tool access; enter
    on the task list edits queued tasks and ctrl+r runs them, each in a fresh agent
    that fetches it from orbit over MCP; verification steps run when it says it's done,
    and failures go back to it to fix.
    """
    if connect is not None:
        _refuse_serve_flags(ctx)
        token_path = token_file or TokenFile(resolve_config_dir(config_dir, os.environ)).path
        opening = NewSession(name=name) if new or name is not None else None
        _launch(_connected_app(connect, token_path, opening), None)
        return
    # The only place the backend and frontend meet, and where the backend's external
    # adapters are plugged into its ports. Every adapter works relative to the current
    # directory, so move into the workspace before anything reads the filesystem.
    profile = profile.expanduser().resolve() if profile else None
    state = resolve_state_dir(state, os.environ)
    user_config = resolve_config_dir(config_dir, os.environ)
    root = _workspace(workspace)
    os.chdir(root)
    backend = _Backend(model, tools, verify, worktree, review, usage, new, name)
    build, store = _backend(backend, root, state, user_config, pick_session=not backend.fresh)
    session = ThreadedSession(build, _thread_context(profile))
    _launch_with(OrbitApp(session), profile, store)


def _refuse_serve_flags(ctx: typer.Context) -> None:
    """Exit with status 2 if any of the flags that configure the control plane were given."""
    given = [f"--{name}" for name in _SERVE_FLAGS if _given(ctx, name)]
    if given:
        typer.echo(
            f"{', '.join(given)} configure the control plane, so they belong on orbit serve,"
            " not orbit chat --connect.",
            err=True,
        )
        raise typer.Exit(2)


def _given(ctx: typer.Context, name: str) -> bool:
    """Whether the option ``name`` was given, not left at its default."""
    source = ctx.get_parameter_source(name)  # Typer's own click, so its enum is compared by name.
    return source is not None and source.name != "DEFAULT"


def _connected_app(url: str, token_file: Path, opening: NewSession | None) -> OrbitApp:
    """The TUI over the control plane at ``url``, shown the token in ``token_file``, sending
    ``opening``, if any, once welcomed.

    Exits with status 2 if ``url`` isn't a ws:// URL or the token can't be read.
    """
    if not url.startswith("ws://"):
        typer.echo(
            f"orbit chat --connect takes a ws:// URL, not {url}; wss://, with TLS, comes in a"
            " later release.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        token = read_token(token_file)
    except TokenUnreadable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    client = WebSocketSession(url, token, token_file)
    return OrbitApp(client, client, opening)


def _backend(
    backend: _Backend, root: Path, state: Path, user_config: Path, *, pick_session: bool
) -> tuple[Callable[[], AgentSession], SqliteStore]:
    """What builds the session ``backend`` asks for in the workspace at ``root``, with the
    store in ``state`` and the user config in ``user_config``, listing the saved sessions to
    pick from if ``pick_session``, and the store.

    Exits with status 2 if a configuration is invalid or the store can't be opened.
    """
    config = _config()
    storage = _storage(user_config)
    verification = _verification(config) if backend.verify else None
    agents = _agents(config, worktree=backend.worktree, review=backend.review)
    settings, pinned = _settings(backend.model, backend.tools)
    usage_source = UsageApi() if backend.usage else None
    store, notes = _open_store(state, root)

    def build() -> AgentSession:
        return AgentSession(
            settings,
            model_choices=MODEL_ALIASES,
            verification=verification,
            agents=agents,
            workspace=str(root),
            check_at_startup=True,
            usage=usage_source,
            store=store,
            pinned=pinned,
            pick_session=pick_session,
            name=backend.name,
            startup_notes=notes,
            storage=storage,
            fallback=FallbackFiles(state, store.workspace_id, orbit_dir(str(root))),
        )

    return build, store


HostOption = Annotated[
    str | None,
    typer.Option(
        help="The address to listen on (default: [server] host in your config.toml, else"
        " 127.0.0.1); only loopback for now."
    ),
]
PortOption = Annotated[
    int | None,
    typer.Option(
        min=0,
        max=65535,
        help="The port to listen on (default: [server] port in your config.toml, else 7433);"
        " 0 lets the system choose one.",
    ),
]


@app.command()
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
) -> None:
    """Run the control plane on its own, for frontends to connect to over a WebSocket.

    It opens the workspace's most recently active session (or a new one with --new or
    --name), and its work goes on with no frontend connected. Frontends show the token in
    the token file in your config directory. The first ctrl+c stops it, cancelling the work
    in progress; a second exits at once.
    """
    profile = profile.expanduser().resolve() if profile else None
    state = resolve_state_dir(state, os.environ)
    user_config = resolve_config_dir(config_dir, os.environ)
    root = _workspace(workspace)
    os.chdir(root)
    settings = _server_settings(user_config, host, port)
    token_file = TokenFile(user_config)
    token = _token(token_file)
    backend = _Backend(model, tools, verify, worktree, review, usage, new, name)
    build, store = _backend(backend, root, state, user_config, pick_session=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parts = _ServeParts(
        build,
        not backend.fresh,
        settings,
        token,
        RunFile(state, store.workspace_id),
        f"Serving {root} at {{url}}; frontends show the token in {token_file.path}.",
    )
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
    typer.echo("orbit serve stopped.")


def _server_settings(config_dir: Path, host: str | None, port: int | None) -> ServerSettings:
    """The server settings in the user config in ``config_dir``, with ``host`` and ``port``
    over them, if given.

    Exits with status 2 if the file is invalid, or the host isn't loopback.
    """
    try:
        settings = read_server(config_dir)
    except ConfigError as exc:
        typer.echo(f"Invalid orbit configuration: {exc}", err=True)
        raise typer.Exit(2) from None
    given = {"host": host, "port": port}
    settings = settings.model_copy(update={k: v for k, v in given.items() if v is not None})
    if not is_loopback(settings.host):
        typer.echo(
            f"orbit serve listens on loopback alone for now, not on {settings.host}; TLS, for"
            " listening beyond it, comes in a later release. Use an SSH tunnel meanwhile.",
            err=True,
        )
        raise typer.Exit(2)
    return settings


def _token(token_file: TokenFile) -> str:
    """The token in ``token_file``, made if there's none.

    Exits with status 2 if it can't be used.
    """
    try:
        return token_file.token()
    except TokenError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


@dataclass(frozen=True)
class _ServeParts:
    """What ``orbit serve`` serves: the session ``build`` makes, with the most recently active
    saved one opened if ``latest``, on the server ``settings`` say, to frontends showing
    ``token``, saying where it is in ``run_file`` while it does, and the ``banner`` it prints
    once listening, with ``{url}`` in it for the server's URL."""

    build: Callable[[], AgentSession]
    latest: bool
    settings: ServerSettings
    token: str
    run_file: RunFile
    banner: str


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
    SIGTERM, then stop it, removing the run file."""
    stopping = asyncio.Event()
    _on_signals(stopping)
    try:
        parts.run_file.write(url, os.getpid())
        typer.echo(parts.banner.format(url=url))
        await stopping.wait()
    finally:
        try:
            await server.stop(session.close)
        finally:
            parts.run_file.remove()


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


def _config() -> OrbitConfig:
    """The project file's configuration.

    Exits with status 2 if the file is invalid.
    """
    try:
        return read_config()
    except ConfigError as exc:
        typer.echo(f"Invalid orbit configuration: {exc}", err=True)
        raise typer.Exit(2) from None


def _storage(config_dir: Path) -> StorageSettings:
    """The storage settings in the user config in ``config_dir``.

    Exits with status 2 if the file is invalid.
    """
    try:
        return read_storage(config_dir)
    except ConfigError as exc:
        typer.echo(f"Invalid orbit configuration: {exc}", err=True)
        raise typer.Exit(2) from None


def _open_store(state: Path, root: Path) -> tuple[SqliteStore, list[str]]:
    """The store in the ``state`` directory for the workspace at ``root``, with the sessions an
    older orbit saved as files and the events an earlier run parked in fallback files imported,
    and the notes on what was imported.

    Exits with status 2 if the store can't be opened, the old sessions can't be imported or a
    fallback file can't be recovered.
    """
    try:
        return asyncio.run(_opened_store(state, root))
    except (StoreError, ImportBlocked, RecoveryFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


async def _opened_store(state: Path, root: Path) -> tuple[SqliteStore, list[str]]:
    """The store in ``state`` for ``root``, with the old session files and the fallback files
    imported, and the notes on what was imported; the store is closed if an import fails."""
    store = await SqliteStore.open(state, str(root))
    try:
        return store, await _imported(store, state, root)
    except BaseException:
        await store.close()
        raise


async def _imported(store: SqliteStore, state: Path, root: Path) -> list[str]:
    """Import into ``store`` the old session files of the workspace at ``root``, then its
    fallback files, in ``state`` and in the repository; returns the notes on what was
    imported."""
    legacy = legacy_dir(str(root))
    imported = await import_session_files(store, legacy)
    notes = [] if imported is None else [imported_note(imported, _shown(legacy, root))]
    recovered = await recover_parked(
        FallbackFiles(state, store.workspace_id, orbit_dir(str(root))), store
    )
    return notes + [
        recovered_note(file.count, str(_shown(Path(file.path), root)))
        for file in recovered
        if file.count
    ]


def _shown(path: Path, root: Path) -> Path:
    """``path`` as shown to the user: relative to the workspace at ``root``, if it's inside."""
    return path.relative_to(root) if path.is_relative_to(root) else path


def _verification(config: OrbitConfig) -> Verification:
    """What verifies tasks with ``config``'s steps, run as processes."""
    steps = config.verification.steps
    return Verification(steps, run_step, workspace_state, config.fallback_fixes)


def _settings(model: str | None, tools: ToolAccess | None) -> tuple[Settings, list[str]]:
    """The settings ``model`` and ``tools`` ask for, and the names of those given, which hold
    even in a session saved with other settings."""
    settings = Settings(model=model, tool_access=tools or "restricted")
    return settings, [name for name, given in (("model", model), ("tool_access", tools)) if given]


def _thread_context(profile: Path | None) -> Callable[[], AbstractContextManager[None]] | None:
    """What the backend's thread runs inside: profiled into ``profile``, if there is one."""
    if profile is None:
        return None
    return lambda: profiled(profile, "backend")


def _agents(config: OrbitConfig, *, worktree: bool, review: bool) -> Agents:
    """The agents that run tasks as ``config`` says, in the coder tree if ``worktree`` and
    the workspace is a git work tree, reviewing landed work if ``review``."""
    tree = GitCoderTree(".", config.setup) if worktree and in_git_work_tree(".") else None
    return Agents(
        McpHost(),
        ClaudeCliRunner(),
        secrets.token_urlsafe,
        config.roles,
        config.limits,
        tree=tree,
        review=review,
        review_limits=config.review_limits,
        write_config=write_checks,
        planner_role=_planner_role,
    )


def _planner_role() -> Role | ConfigError:
    """The planner's role as the project file says now, or why the file is invalid."""
    try:
        return read_config().roles[PLANNER]
    except ConfigError as exc:
        return exc


def _launch_with(tui: OrbitApp, profile: Path | None, store: SqliteStore) -> None:
    """Run ``tui`` as :func:`_launch` does, then close ``store``."""
    try:
        _launch(tui, profile)
    finally:  # The app has closed its session, so nothing uses the store any more.
        asyncio.run(store.close())


def _launch(tui: OrbitApp, profile: Path | None) -> None:
    """Run ``tui``, profiled into the ``profile`` directory if there is one, then print why
    its session is halted, if it is, as the halt's modal no longer shows it."""
    if profile is None:
        tui.run()
    else:
        asyncio.run(_run_profiled(tui, profile))
    if tui.storage_halt is not None:
        typer.echo(tui.storage_halt, err=True)


async def _run_profiled(tui: OrbitApp, directory: Path) -> None:
    """Run ``tui``, profiling its thread into ``directory``."""
    with profiled(directory, "ui"):
        await tui.run_async()


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


@db.command()
def backup(
    path: Annotated[Path, typer.Argument(help="The file to write the backup to.")],
    force: Annotated[bool, typer.Option("--force", help="Replace PATH if it exists.")] = False,
    state: StateDirOption = None,
) -> None:
    """Back up the database of saved sessions to PATH, safely even while orbit runs."""
    target = path.expanduser().absolute()
    if target.exists() and not force:
        typer.echo(f"{target} already exists; pass --force to replace it.", err=True)
        raise typer.Exit(2)
    try:
        asyncio.run(_backed_up(resolve_state_dir(state, os.environ), target))
    except StoreError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo(f"Backed up orbit's database to {target}.")


async def _backed_up(state: Path, target: Path) -> None:
    """Back up the database in ``state`` to ``target``, registering and importing nothing."""
    store = await SqliteStore.open_for_backup(state)
    try:
        await store.backup(target)
    finally:
        await store.close()
