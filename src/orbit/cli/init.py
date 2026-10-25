"""orbit init: a workspace's .orbit/config.toml started from orbit's reference config, or brought
up to date with it."""

from pathlib import Path
from typing import Annotated

import typer

from orbit.agent import (
    INVALID_CONFIG,
    ConfigError,
    ConfigExists,
    InitResult,
    UpdateResult,
    init_config,
    update_config,
)
from orbit.cli.backend import _workspace
from orbit.cli.errors import _exit_on
from orbit.cli.options import WorkspaceOption

CONFIG_EXISTS = (
    "{path} already exists. Use orbit init --update to add the settings it doesn't mention, or"
    " --force to replace it (the old file is kept as a backup)."
)

UPDATE_WITH_FORCE = "--update keeps the file and --force replaces it, so they can't go together."

WRITTEN = "Every setting is commented out at its default; uncomment what you change."

NOTHING_TO_ADD = "Nothing to add."

WRITE_FAILED = "Couldn't write orbit's reference config: "

UPDATE_FAILED = "Couldn't update the config from orbit's reference config: "

InitUpdateOption = Annotated[
    bool,
    typer.Option(
        "--update",
        help="Add the settings an existing .orbit/config.toml doesn't mention, commented out,"
        " without changing any of its lines.",
    ),
]

InitForceOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Replace an existing .orbit/config.toml, keeping it as the next free"
        " config.toml.bak.N beside it.",
    ),
]


def init(
    workspace: WorkspaceOption = Path("."),
    update: InitUpdateOption = False,
    force: InitForceOption = False,
) -> None:
    """Write orbit's reference config to the workspace's .orbit/config.toml: every setting a
    project can change, commented out at its default under what it does.

    An existing file is left as it is, exiting with status 2, unless --update adds the settings
    it doesn't mention, commented out, each key in its table and each missing table at the end,
    or --force replaces it, after keeping it as config.toml.bak.1, or .bak.2 and so on if that's
    taken.
    """
    if update and force:
        typer.echo(UPDATE_WITH_FORCE, err=True)
        raise typer.Exit(2)
    root = _workspace(workspace)
    failed = UPDATE_FAILED if update else WRITE_FAILED
    with (
        _exit_on(ConfigExists, message=lambda exc: CONFIG_EXISTS.format(path=exc.path)),
        _exit_on(ConfigError, message=lambda exc: f"{INVALID_CONFIG}{exc}"),
        _exit_on(OSError, message=lambda exc: f"{failed}{exc}"),
    ):
        said = _updated(update_config(root)) if update else _written(init_config(root, force))
    typer.echo(said)


def _written(result: InitResult) -> str:
    """What's said once ``result`` is written: where, where the old file was kept, and that
    every setting is commented out."""
    written = f"orbit's reference config to {result.path}."
    if result.backup is None:
        return f"Wrote {written}\n{WRITTEN}"
    return f"Kept the old file as {result.backup} and wrote {written}\n{WRITTEN}"


def _updated(result: UpdateResult) -> str:
    """What's said once ``result`` is done: what was added to the file, and where, or that
    there was nothing to add; that the reference was written, if there was no file."""
    if result.additions is None:
        return _written(InitResult(result.path))
    added = _added(result.additions.keys, result.additions.tables)
    if not added:
        return NOTHING_TO_ADD
    return f"Added to {result.path}, commented out:" + "".join(f"\n  {line}" for line in added)


def _added(keys: dict[str, tuple[str, ...]], tables: tuple[str, ...]) -> list[str]:
    """A line per table the ``keys`` were added to, by its header, then one of the ``tables``
    appended at the end, if there are any."""
    lines = [
        f"in {header or 'the top level'}: {', '.join(added)}" for header, added in keys.items()
    ]
    return [*lines, f"at the end: {', '.join(tables)}"] if tables else lines
