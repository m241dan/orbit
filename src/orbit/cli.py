"""Command-line interface for orbit."""

from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import AgentSession, ClaudeCliRunner
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
) -> None:
    """Start an interactive agent session.

    In the app: ctrl+o / ctrl+l open a picker for the model / tool access.
    """
    # The only place the backend and frontend meet.
    session = AgentSession(
        ClaudeCliRunner(),
        Settings(model=model, tool_access=tools),
        model_choices=MODEL_ALIASES,
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
