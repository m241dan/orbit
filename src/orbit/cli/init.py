"""orbit init: a workspace's .orbit/config.toml started from orbit's reference config, brought
up to date with it, or given copies of the built-in roles' instructions to edit."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from orbit.agent import (
    INVALID_CONFIG,
    ConfigError,
    ConfigExists,
    InitResult,
    InstructionLines,
    RolesResult,
    UpdateResult,
    add_roles,
    init_config,
    instructions_setting,
    update_config,
)
from orbit.cli.backend import _workspace
from orbit.cli.errors import _exit_on
from orbit.cli.options import WorkspaceOption

CONFIG_EXISTS = (
    "{path} already exists. Use orbit init --update to add the settings it doesn't mention, or"
    " --force to replace it (the old file is kept as a backup)."
)

WITH_FORCE = "{option} keeps the file and --force replaces it, so they can't go together."

WRITTEN = "Every setting is commented out at its default; uncomment what you change."

NOTHING_TO_ADD = "Nothing to add."

WRITE_FAILED = "Couldn't write orbit's reference config: "

UPDATE_FAILED = "Couldn't update the config from orbit's reference config: "

ROLES_FAILED = "Couldn't copy the built-in roles' instructions: "

SWITCHES = "Uncommenting a role's instructions line switches that role to its copy."

NO_LINES = "Each role's table already has an instructions line."

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


InitRolesOption = Annotated[
    bool,
    typer.Option(
        "--roles",
        help="Copy each built-in role's instructions to .orbit/roles/<name>.md to edit, and add"
        " a commented-out line to its table that switches it to its copy once uncommented.",
    ),
]


def init(
    workspace: WorkspaceOption = Path("."),
    update: InitUpdateOption = False,
    force: InitForceOption = False,
    roles: InitRolesOption = False,
) -> None:
    """Write orbit's reference config to the workspace's .orbit/config.toml: every setting a
    project can change, commented out at its default under what it does.

    An existing file is left as it is, exiting with status 2, unless --update adds the settings
    it doesn't mention, commented out, each key in its table and each missing table at the end,
    or --force replaces it, after keeping it as config.toml.bak.1, or .bak.2 and so on if that's
    taken.

    --roles, by itself or after --update, copies each built-in role's instructions to
    .orbit/roles/<name>.md, keeping a file already there, and adds to each role's table without
    an instructions line a commented-out one naming the copy, writing the reference first if
    there's no file; the role keeps orbit's own instructions until the line is uncommented.
    """
    _refuse_with_force(force, update=update, roles=roles)
    root = _workspace(workspace)
    with (
        _exit_on(ConfigExists, message=lambda exc: CONFIG_EXISTS.format(path=exc.path)),
        _exit_on(ConfigError, message=lambda exc: f"{INVALID_CONFIG}{exc}"),
    ):
        if update:
            _say(UPDATE_FAILED, lambda: _updated(update_config(root)))
        if roles:
            _say(ROLES_FAILED, lambda: _copied(add_roles(root), root))
        if not update and not roles:
            _say(WRITE_FAILED, lambda: _written(init_config(root, force)))


def _refuse_with_force(force: bool, update: bool, roles: bool) -> None:
    """Exit with status 2 if ``force`` is given with ``update`` or ``roles``, which keep the
    file it replaces."""
    for given, option in ((update, "--update"), (roles, "--roles")):
        if force and given:
            typer.echo(WITH_FORCE.format(option=option), err=True)
            raise typer.Exit(2)


def _say(failed: str, step: Callable[[], str]) -> None:
    """Say what ``step`` says once done, exiting with status 2 after ``failed`` and the error if
    it raises ``OSError``."""
    with _exit_on(OSError, message=lambda exc: f"{failed}{exc}"):
        said = step()
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
    and keys appended at the end, if there are any."""
    lines = [
        f"in {header or 'the top level'}: {', '.join(added)}" for header, added in keys.items()
    ]
    return [*lines, f"at the end: {', '.join(tables)}"] if tables else lines


def _copied(result: RolesResult, root: Path) -> str:
    """What's said once ``result`` is done in the workspace at ``root``: that the reference was
    written, if it was; each role's instructions copied or file kept; and the lines added to
    the file, and what uncommenting one does, or that there were none to add."""
    said = [_written(InitResult(result.path))] if result.wrote else []
    said += [
        f"Copied the {name}'s instructions to {path.relative_to(root)}."
        for name, path in result.copied.items()
    ]
    said += [f"Kept your {path.relative_to(root)}." for path in result.kept.values()]
    return "\n".join([*said, *_lines_added(result.path, result.lines)])


def _lines_added(path: Path, lines: InstructionLines) -> list[str]:
    """A line per role whose table at ``path`` was given ``lines``' instructions line, then one
    per role whose table was appended with it, under where and over what uncommenting one does;
    that there were none, if there weren't."""
    if not lines.added and not lines.appended:
        return [NO_LINES]
    added = [f"  in [roles.{name}]: {instructions_setting(name)}" for name in lines.added]
    appended = [
        f"  at the end: [roles.{name}], with {instructions_setting(name)}"
        for name in lines.appended
    ]
    return [f"Added to {path}, commented out:", *added, *appended, SWITCHES]
