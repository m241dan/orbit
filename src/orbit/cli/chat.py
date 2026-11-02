"""orbit chat: the TUI over a control plane run here, or one it joins."""

import asyncio
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import typer

from orbit.agent import resolve_config_dir, resolve_state_dir
from orbit.cli.backend import _Backend, _backend, _Directories, _in_process, _workspace
from orbit.cli.errors import _configured, _exit_on
from orbit.cli.joining import (
    LOCAL_ERRORS,
    ClientFiles,
    Discovered,
    Joining,
    discover_control_plane,
    joined_client,
    joining_for,
    named_target,
    option_given,
    refuse_state_dir,
)
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
    RuntimeOption,
    StateDirOption,
    TokenFileOption,
    ToolsOption,
    UsageOption,
    VerifyOption,
    WebOption,
    WorkspaceOption,
    WorktreeOption,
)
from orbit.cli.runtimes import given_runtime
from orbit.cli.telemetry import _agent_collector, _refuse_console, _telemetry
from orbit.client import HandshakeRefused, TokenRejected, messages
from orbit.execution.threads import ThreadedSession
from orbit.profiling import profiled
from orbit.protocol import CommandEnvelope, EventEnvelope, NewSession
from orbit.protocol.transport import is_loopback
from orbit.telemetry import TelemetryRun
from orbit.tracing import Telemetry
from orbit.tui import Opening, OrbitApp


def chat(
    # #lizard forgives(parameter_count): Typer takes each of the command's options as a parameter.
    ctx: typer.Context,
    workspace: WorkspaceOption = Path("."),
    model: ModelOption = None,
    tools: ToolsOption = None,
    web: WebOption = None,
    runtime: RuntimeOption = None,
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
    ctrl+o / ctrl+l open a picker for the planner's model / new tasks' workspace level; enter
    on the task list edits queued tasks and ctrl+r runs them, each in a fresh agent
    that fetches it from orbit over MCP; verification steps run when it says it's done,
    and failures go back to it to fix.
    """
    user_config = resolve_config_dir(config_dir, os.environ)
    _refuse_console(user_config)
    given = ClientFiles(token_file, ca, cert, key)
    flags = _Backend(
        model, tools, web, verify, worktree, review, usage, new, name, given_runtime(runtime)
    )
    target = named_target(connect, remote, user_config, given)
    state = resolve_state_dir(state, os.environ)
    run = TelemetryRun("orbit-chat", user_config, state, os.environ)
    if target is not None:
        _refuse_local(local, target.flag)
        refuse_state_dir(ctx, target.flag)
        joining = joining_for(ctx, target.files, user_config, _opening(flags))
        where = f"not orbit chat {target.flag}"
        repository = _repository_for(target.url, workspace)
        _join(replace(joining, repository=repository), target.url, where, run)
        return
    root = _workspace(workspace)
    found = None if local else _discovered(state, given, user_config)
    if found is not None:
        joining = joining_for(ctx, found.files, user_config, _opening(flags))
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
            session = ThreadedSession[CommandEnvelope, EventEnvelope](
                backend.build, _thread_context(profile)
            )
            opening = Opening(pick=picking, repository=str(root))
            _launch(OrbitApp(session, opening=opening, telemetry=telemetry), profile)


def on_this_machine(url: str) -> bool:
    """Whether the control plane at ``url`` runs on this machine, as its host is loopback, so
    a path here names the same directory there."""
    return is_loopback(urlsplit(url).hostname or "")


def _repository_for(url: str, workspace: Path) -> str | None:
    """The repository at ``workspace`` for the workspace picker of the control plane at ``url``
    to offer; ``None`` unless it runs on this machine, where the path names the same one."""
    return str(_workspace(workspace)) if on_this_machine(url) else None


def _opening(flags: _Backend) -> NewSession | None:
    """The new session the backend ``flags`` ask a joining chat to open, if any."""
    return NewSession(name=flags.name) if flags.fresh else None


def _refuse_client_files(given: ClientFiles, reason: str) -> None:
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
:class:`ClientFiles` they set."""


def _refuse_local(local: bool, flag: str) -> None:
    """Exit with status 2 if ``local`` was asked for along with ``flag``, --connect or
    --remote."""
    if local:
        typer.echo(f"--local runs the control plane here, so it can't go with {flag}.", err=True)
        raise typer.Exit(2)


def _join(joining: Joining, url: str, where: str, run: TelemetryRun) -> None:
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


def _discovered(state: Path, given: ClientFiles, config_dir: Path) -> Discovered | None:
    """What :func:`discover_control_plane` finds, shown the files ``given``; a control plane
    found is never bypassed.

    Exits with status 2 if the token, the user config, the CA or the client certificate can't
    be used, the control plane turns the token away, or refuses the handshake.
    """
    refused = messages.LOCAL_TOKEN_REFUSED.format(path=given.token_path(config_dir))
    with (
        _exit_on(TokenRejected, message=lambda _: refused),
        _exit_on(HandshakeRefused, message=_offering_local),
        _exit_on(*LOCAL_ERRORS),
    ):
        return _configured(lambda: discover_control_plane(state, given, config_dir))


def _offering_local(refusal: HandshakeRefused) -> str:
    """What says the control plane found here refused the handshake, as ``refusal`` says,
    offering --local instead."""
    return f"{str(refusal).removesuffix('.')}; or pass --local to run one here."


def _refuse_serve_flags(ctx: typer.Context, where: str) -> None:
    """Exit with status 2 if any of the flags that configure the control plane were given,
    saying they belong on orbit serve, ``where`` saying which."""
    given = [f"--{name}" for name in _SERVE_FLAGS if option_given(ctx, name)]
    if given:
        typer.echo(
            f"{', '.join(given)} configure the control plane, so they belong on orbit serve,"
            f" {where}.",
            err=True,
        )
        raise typer.Exit(2)


def _connected_app(url: str, joining: Joining, telemetry: Telemetry) -> OrbitApp:
    """The TUI over the control plane at ``url``, joined as ``joining`` says, tracing its
    actions with ``telemetry``.

    Exits with status 2 if ``url`` is a ws:// one beyond loopback, or the token, the CA or the
    client certificate can't be read.
    """
    client = joined_client(url, joining)
    opening = Opening(joining.opening, joining.opening is None, joining.repository)
    return OrbitApp(client, client, opening, telemetry=telemetry)


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
