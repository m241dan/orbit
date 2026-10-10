"""Command-line interface for orbit."""

from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import AgentSession, EchoModel
from orbit.tui import OrbitApp

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)


@app.command()
def chat() -> None:
    """Start an interactive agent session."""
    # The only place the backend and frontend meet.
    session = AgentSession(model=EchoModel())
    OrbitApp(session).run()


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


@app.command()
def hello(name: Annotated[str, typer.Argument(help="Who to greet.")] = "world") -> None:
    """Say hello."""
    typer.echo(f"Hello, {name}!")
