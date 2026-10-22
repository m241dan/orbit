"""How the CLI's commands exit on errors."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import typer

from orbit.agent import INVALID_CONFIG, ConfigError


@contextmanager
def _exit_on[E: Exception](*errors: type[E], message: Callable[[E], str] = str) -> Iterator[None]:
    """Run the block, exiting with status 2 if it raises one of ``errors``, after saying
    ``message`` of the error."""
    try:
        yield
    except errors as exc:
        typer.echo(message(exc), err=True)
        raise typer.Exit(2) from None


def _configured[T](read: Callable[[], T]) -> T:
    """What ``read`` reads from orbit's configuration.

    Exits with status 2 if the configuration is invalid.
    """
    with _exit_on(ConfigError, message=lambda exc: f"{INVALID_CONFIG}{exc}"):
        return read()
