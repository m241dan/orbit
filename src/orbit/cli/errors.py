"""How the CLI's commands exit on errors."""

from collections.abc import Iterator
from contextlib import contextmanager

import typer


@contextmanager
def _exit_on(*errors: type[Exception], prefix: str = "") -> Iterator[None]:
    """Run the block, exiting with status 2 if it raises one of ``errors``, after saying
    ``prefix`` and the error."""
    try:
        yield
    except errors as exc:
        typer.echo(f"{prefix}{exc}", err=True)
        raise typer.Exit(2) from None
