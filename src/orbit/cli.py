"""Command-line interface for orbit."""

import os
import secrets
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
    Limits,
    McpHost,
    Verification,
    git_top_level,
    in_git_work_tree,
    read_config,
    run_step,
    workspace_state,
    write_checks,
)
from orbit.protocol import Settings, ToolAccess
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
        ToolAccess,
        typer.Option(
            help="Default tool access for new tasks: off (none), restricted (read-only; anything"
            " needing approval is refused), full (auto-approve everything)."
        ),
    ] = "restricted",
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
) -> None:
    """Start an interactive agent session.

    In the app: h/j/k/l move between windows; i jumps to the prompt, which talks to
    the planner, whose proposals wait on the task list as drafts, and esc stops;
    ctrl+o / ctrl+l open a picker for the planner's model / new tasks' tool access; enter
    on the task list edits queued tasks and ctrl+r runs them, each in a fresh session.
    Each task runs in a fresh agent that fetches it from orbit over MCP; verification
    steps run when it says it's done, and failures go back to it to fix.
    """
    # The only place the backend and frontend meet, and where the backend's external
    # adapters are plugged into its ports. Every adapter works relative to the current
    # directory, so move into the workspace before anything reads the filesystem.
    root = _workspace(workspace)
    os.chdir(root)
    try:
        config = read_config()
    except ConfigError as exc:
        typer.echo(f"Invalid orbit configuration: {exc}", err=True)
        raise typer.Exit(2) from None
    steps = config.verification.steps
    verification = Verification(steps, run_step, workspace_state) if verify else None
    tree = GitCoderTree(".", config.setup) if worktree and in_git_work_tree(".") else None
    agents = Agents(
        McpHost(),
        ClaudeCliRunner(),
        secrets.token_urlsafe,
        Limits(max_attempts=config.fix_attempts),
        tree=tree,
        roles=config.roles,
        review=review,
        review_rounds=config.review_rounds,
        review_suggestions=config.review_suggestions,
        super_review_every=config.super_review_every,
        super_review_suggestions=config.super_review_suggestions,
        write_config=write_checks,
    )
    session = AgentSession(
        Settings(model=model, tool_access=tools),
        model_choices=MODEL_ALIASES,
        verification=verification,
        agents=agents,
        workspace=str(root),
        check_at_startup=True,
    )
    OrbitApp(session).run()


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


@app.command()
def hello(name: Annotated[str, typer.Argument(help="Who to greet.")] = "world") -> None:
    """Say hello."""
    typer.echo(f"Hello, {name}!")
