"""Command-line interface for orbit."""

import asyncio
import os
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import (
    MODEL_ALIASES,
    Agents,
    AgentSession,
    ClaudeCliRunner,
    ConfigError,
    GitCoderTree,
    McpHost,
    OrbitConfig,
    SessionFiles,
    UsageApi,
    Verification,
    git_top_level,
    in_git_work_tree,
    read_config,
    run_step,
    state_dir,
    workspace_state,
    write_checks,
)
from orbit.profiling import profiled
from orbit.protocol import Settings, ThreadedSession, ToolAccess
from orbit.tui import OrbitApp

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)


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


@app.command()
def chat(
    workspace: Annotated[
        Path,
        typer.Option(
            "-C",
            "--workspace",
            help="The project to work on (default: the current directory). Inside a git"
            " repository, its top level.",
        ),
    ] = Path("."),
    model: Annotated[
        str | None,
        typer.Option(
            help=f"The planner's model: an alias ({', '.join(MODEL_ALIASES)}) or full model name."
        ),
    ] = None,
    tools: Annotated[
        ToolAccess | None,
        typer.Option(
            help="Default tool access for new tasks: off (none), restricted (read-only; anything"
            " needing approval is refused; the default), full (auto-approve everything)."
        ),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            help="Run the verification steps in .orbit/config.toml once at startup, and after"
            " each task; a failure after a task goes back to the agent to fix."
        ),
    ] = True,
    worktree: Annotated[
        bool,
        typer.Option(
            help="Run tasks in a separate git worktree and merge their verified commits into"
            " the current branch (only inside a git repository).",
        ),
    ] = True,
    review: Annotated[
        bool,
        typer.Option(
            help="Review each landed commit in the background and queue the reviewer's"
            " suggestions as tasks (needs the worktree).",
        ),
    ] = True,
    usage: Annotated[
        bool,
        typer.Option(
            help="Show your Claude subscription's session and weekly usage limits, read with"
            " your Claude login's token from ~/.claude/.credentials.json.",
        ),
    ] = True,
    new: NewOption = False,
    name: NameOption = None,
    profile: ProfileOption = None,
) -> None:
    """Start an interactive agent session.

    At startup, pick a saved session or start a new one (or start one with --new); ctrl+p
    switches later.

    In the app: h/j/k/l move between windows; i jumps to the prompt, which talks to
    the planner, whose proposals wait on the task list as drafts, and ctrl+s stops the run;
    ctrl+o / ctrl+l open a picker for the planner's model / new tasks' tool access; enter
    on the task list edits queued tasks and ctrl+r runs them, each in a fresh session.
    Each task runs in a fresh agent that fetches it from orbit over MCP; verification
    steps run when it says it's done, and failures go back to it to fix.
    """
    # The only place the backend and frontend meet, and where the backend's external
    # adapters are plugged into its ports. Every adapter works relative to the current
    # directory, so move into the workspace before anything reads the filesystem.
    profile = profile.expanduser().resolve() if profile else None
    root = _workspace(workspace)
    os.chdir(root)
    try:
        config = read_config()
    except ConfigError as exc:
        typer.echo(f"Invalid orbit configuration: {exc}", err=True)
        raise typer.Exit(2) from None
    verification = _verification(config) if verify else None
    agents = _agents(config, worktree=worktree, review=review)
    settings, pinned = _settings(model, tools)
    usage_source = UsageApi() if usage else None
    session = ThreadedSession(
        lambda: AgentSession(
            settings,
            model_choices=MODEL_ALIASES,
            verification=verification,
            agents=agents,
            workspace=str(root),
            check_at_startup=True,
            usage=usage_source,
            store=SessionFiles(state_dir(str(root))),
            pinned=pinned,
            pick_session=not new and name is None,
            name=name,
        ),
        _thread_context(profile),
    )
    _launch(OrbitApp(session), profile)


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
    )


def _launch(tui: OrbitApp, profile: Path | None) -> None:
    """Run ``tui``, profiled into the ``profile`` directory if there is one."""
    if profile is None:
        tui.run()
    else:
        asyncio.run(_run_profiled(tui, profile))


async def _run_profiled(tui: OrbitApp, directory: Path) -> None:
    """Run ``tui``, profiling its thread into ``directory``."""
    with profiled(directory, "ui"):
        await tui.run_async()


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)
