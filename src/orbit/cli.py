"""Command-line interface for orbit."""

from typing import Annotated

import typer

from orbit import __version__

app = typer.Typer(help="Orbit command-line tool.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


@app.command()
def hello(name: Annotated[str, typer.Argument(help="Who to greet.")] = "world") -> None:
    """Say hello."""
    typer.echo(f"Hello, {name}!")
