"""orbit init: a workspace's .orbit/config.toml started from orbit's reference config."""

from pathlib import Path
from typing import Annotated

import typer

from orbit.agent import ConfigExists, InitResult, init_config
from orbit.cli.backend import _workspace
from orbit.cli.errors import _exit_on
from orbit.cli.options import WorkspaceOption

CONFIG_EXISTS = (
    "{path} already exists. Use orbit init --update to add the settings it doesn't mention, or"
    " --force to replace it (the old file is kept as a backup)."
)

WRITTEN = "Every setting is commented out at its default; uncomment what you change."

InitForceOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Replace an existing .orbit/config.toml, keeping it as the next free"
        " config.toml.bak.N beside it.",
    ),
]


def init(workspace: WorkspaceOption = Path("."), force: InitForceOption = False) -> None:
    """Write orbit's reference config to the workspace's .orbit/config.toml: every setting a
    project can change, commented out at its default under what it does.

    An existing file is left as it is, exiting with status 2, unless --force replaces it, after
    keeping it as config.toml.bak.1, or .bak.2 and so on if that's taken.
    """
    root = _workspace(workspace)
    with (
        _exit_on(ConfigExists, message=lambda exc: CONFIG_EXISTS.format(path=exc.path)),
        _exit_on(OSError, message=lambda exc: f"Couldn't write orbit's reference config: {exc}"),
    ):
        result = init_config(root, force)
    typer.echo(_written(result))
    typer.echo(WRITTEN)


def _written(result: InitResult) -> str:
    """What's said once ``result`` is written: where, and where the old file was kept."""
    written = f"orbit's reference config to {result.path}."
    if result.backup is None:
        return f"Wrote {written}"
    return f"Kept the old file as {result.backup} and wrote {written}"
