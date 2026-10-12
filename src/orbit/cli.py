"""Command-line interface for orbit."""

from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import DEFAULT_STEPS, AgentSession, ClaudeCliRunner, workspace_state
from orbit.agent.claude_cli import MODEL_ALIASES
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
) -> None:
    """Start an interactive agent session.

    In the app: h/j/k/l move between windows; i jumps to the prompt to
    type and esc stops;
    ctrl+o / ctrl+l open a picker for the model / tool access; enter on the
    task list edits queued tasks and ctrl+r runs them, each in a fresh session.
    Verification steps run after each task; a failing one queues a fix task.
    """
    # The only place the backend and frontend meet.
    session = AgentSession(
        ClaudeCliRunner(),
        Settings(model=model, tool_access=tools),
        model_choices=MODEL_ALIASES,
        verification_steps=DEFAULT_STEPS if verify else (),
        workspace=workspace_state,
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
