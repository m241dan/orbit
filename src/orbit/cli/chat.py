"""orbit chat: the TUI over a control plane run here, or one it joins."""

import asyncio
import os
import ssl
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import typer

from orbit.agent import (
    SqliteStore,
    TokenFile,
    resolve_config_dir,
    resolve_state_dir,
    running_server,
)
from orbit.cli.backend import _Backend, _backend, _repository_lock, _workspace
from orbit.cli.errors import _exit_on
from orbit.cli.options import (
    _SERVE_FLAGS,
    CaOption,
    ConfigDirOption,
    ConnectOption,
    LocalOption,
    ModelOption,
    NameOption,
    NewOption,
    ProfileOption,
    ReviewOption,
    StateDirOption,
    TokenFileOption,
    ToolsOption,
    UsageOption,
    VerifyOption,
    WorkspaceOption,
    WorktreeOption,
)
from orbit.client import (
    CaUnreadable,
    PlaintextRefused,
    TokenRejected,
    TokenUnreadable,
    WebSocketSession,
    accepts,
    client_ssl_context,
    read_token,
)
from orbit.profiling import profiled
from orbit.protocol import NewSession, ThreadedSession
from orbit.tui import OrbitApp


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
    ca: CaOption = None,
    local: LocalOption = False,
) -> None:
    """Start an interactive agent session.

    At startup, pick a saved session or start a new one (or start one with --new); ctrl+p
    picks again later. With --connect, or when an orbit serve is running for the workspace
    (unless --local), join it instead, the picker offering to resume the session it has open.

    In the app: h/j/k/l move between windows; i jumps to the prompt, which talks to
    the planner, whose proposals wait on the task list as drafts, and ctrl+s stops the run;
    ctrl+o / ctrl+l open a picker for the planner's model / new tasks' tool access; enter
    on the task list edits queued tasks and ctrl+r runs them, each in a fresh agent
    that fetches it from orbit over MCP; verification steps run when it says it's done,
    and failures go back to it to fix.
    """
    user_config = resolve_config_dir(config_dir, os.environ)
    token_path = token_file or TokenFile(user_config).path
    joining = _Joining(ctx, token_path, ca, _opening(new, name))
    if connect is not None:
        _refuse_local(local)
        _join(joining, connect, "not orbit chat --connect")
        return
    state = resolve_state_dir(state, os.environ)
    root = _workspace(workspace)
    found = None if local else _discovered(state, root, token_path)
    if found is not None:
        _join(joining, found, f"which serves {root} at {found}; or pass --local to run one here")
        return
    # The only place the backend and frontend meet, and where the backend's external
    # adapters are plugged into its ports. Every adapter works relative to the current
    # directory, so move into the workspace before anything reads the filesystem.
    profile = profile.expanduser().resolve() if profile else None
    os.chdir(root)
    with _repository_lock(root) as (lock, owner):
        backend = _Backend(model, tools, verify, worktree, review, usage, new, name, owner, lock)
        build, store = _backend(backend, root, state, user_config, pick_session=not backend.fresh)
        session = ThreadedSession(build, _thread_context(profile))
        _launch_with(OrbitApp(session, pick=not backend.fresh), profile, store)


@dataclass(frozen=True)
class _Joining:
    """How a chat joins a running control plane: with the flags in ``ctx``, showing the token
    in ``token_file``, verifying a ``wss://`` one by the CA in ``ca``, if given, and sending
    ``opening``, if any, once welcomed."""

    ctx: typer.Context
    token_file: Path
    ca: Path | None
    opening: NewSession | None


def _opening(new: bool, name: str | None) -> NewSession | None:
    """The new session ``new`` or ``name`` ask a joining chat to open, if any."""
    return NewSession(name=name) if new or name is not None else None


def _refuse_local(local: bool) -> None:
    """Exit with status 2 if ``local`` was asked for along with --connect."""
    if local:
        typer.echo("--local runs the control plane here, so it can't go with --connect.", err=True)
        raise typer.Exit(2)


def _join(joining: _Joining, url: str, where: str) -> None:
    """Run the TUI over the control plane at ``url``, as ``joining`` says.

    Exits with status 2 if a flag that configures the control plane was given, saying it
    belongs on orbit serve, ``where`` saying which.
    """
    _refuse_serve_flags(joining.ctx, where)
    _launch(_connected_app(url, joining), None)


def discover_control_plane(state: Path, workspace: Path, token_file: Path) -> str | None:
    """The URL of the control plane serving the workspace at ``workspace``, as its run file in
    the ``state`` directory says, if the process it names is alive and it takes the token in
    ``token_file``; ``None`` if there's none, or it can't be reached.

    Raises :class:`TokenUnreadable` if the token can't be read, and :class:`TokenRejected` if
    the control plane turns it away.
    """
    server = running_server(state, str(workspace))
    if server is None:
        return None
    token = read_token(token_file)
    return server.url if asyncio.run(accepts(server.url, token)) else None


def _discovered(state: Path, workspace: Path, token_file: Path) -> str | None:
    """What :func:`discover_control_plane` finds; a control plane found is never bypassed.

    Exits with status 2 if the token can't be read, or the control plane turns it away.
    """
    try:
        with _exit_on(TokenUnreadable):
            return discover_control_plane(state, workspace, token_file)
    except TokenRejected:
        typer.echo(
            f"The control plane serving {workspace} refused the token from {token_file}; give it"
            " the token orbit serve uses, or pass --local to run one here.",
            err=True,
        )
        raise typer.Exit(2) from None


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


def _connected_app(url: str, joining: _Joining) -> OrbitApp:
    """The TUI over the control plane at ``url``, joined as ``joining`` says.

    Exits with status 2 if ``url`` isn't a ws:// or wss:// URL, is a ws:// one beyond loopback,
    or the token or the CA can't be read.
    """
    if not url.startswith(("ws://", "wss://")):
        typer.echo(f"orbit chat --connect takes a wss:// or ws:// URL, not {url}.", err=True)
        raise typer.Exit(2)
    with _exit_on(TokenUnreadable, CaUnreadable, PlaintextRefused):
        read_token(joining.token_file)  # The session reads it again for each connection.
        context = _ssl_context(url, joining.ca)
        client = WebSocketSession(url, joining.token_file, ssl_context=context)
    return OrbitApp(client, client, joining.opening, pick=joining.opening is None)


def _ssl_context(url: str, ca: Path | None) -> ssl.SSLContext | None:
    """What verifies the control plane at ``url``, trusting the CA in ``ca`` if given: none
    for a ``ws://`` URL, which has no TLS.

    Raises :class:`CaUnreadable` if ``ca`` can't be read.
    """
    return client_ssl_context(ca) if url.startswith("wss://") else None


def _thread_context(profile: Path | None) -> Callable[[], AbstractContextManager[None]] | None:
    """What the backend's thread runs inside: profiled into ``profile``, if there is one."""
    if profile is None:
        return None
    return lambda: profiled(profile, "backend")


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
