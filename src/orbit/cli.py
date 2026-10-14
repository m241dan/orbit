"""Command-line interface for orbit."""

import secrets
from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import (
    DEFAULT_STEPS,
    MODEL_ALIASES,
    Agents,
    AgentSession,
    ClaudeCliRunner,
    Limits,
    McpHost,
    Verification,
    run_step,
    workspace_state,
)
from orbit.protocol import Settings, ToolAccess
from orbit.tui import OrbitApp

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)


@app.command()
def chat(
    model: Annotated[
        str | None,
        typer.Option(help=f"Model alias ({', '.join(MODEL_ALIASES)}) or full model name."),
    ] = None,
    tools: Annotated[
        ToolAccess,
        typer.Option(
            help="Tool access: off (none), restricted (read-only; anything needing approval is"
            " refused), full (auto-approve everything)."
        ),
    ] = "restricted",
    verify: Annotated[
        bool,
        typer.Option(
            help="After each task, run the linters, type check, code quality check and unit"
            " tests (100% coverage); a failure queues a task to fix it."
        ),
    ] = True,
    fix_attempts: Annotated[
        int,
        typer.Option(
            min=1,
            help="Fixes asked of the agent working on a task, in the same session, before a"
            " fresh agent takes over.",
        ),
    ] = Limits().max_attempts,
) -> None:
    """Start an interactive agent session.

    In the app: h/j/k/l move between windows; i jumps to the prompt to
    type and esc stops;
    ctrl+o / ctrl+l open a picker for the model / tool access; enter on the
    task list edits queued tasks and ctrl+r runs them, each in a fresh session.
    Each task runs in a fresh agent that fetches it from orbit over MCP; verification
    steps run when it says it's done, and failures go back to it to fix.
    """
    # The only place the backend and frontend meet, and where the backend's external
    # adapters are plugged into its ports.
    verification = Verification(DEFAULT_STEPS, run_step, workspace_state) if verify else None
    agents = Agents(
        McpHost(), ClaudeCliRunner(), secrets.token_urlsafe, Limits(max_attempts=fix_attempts)
    )
    session = AgentSession(
        ClaudeCliRunner(),
        Settings(model=model, tool_access=tools),
        model_choices=MODEL_ALIASES,
        verification=verification,
        agents=agents,
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
