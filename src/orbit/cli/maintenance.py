"""The commands that look after orbit: its version, database and token."""

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from orbit import __version__
from orbit.agent import (
    Purged,
    PurgeRefused,
    SqliteStore,
    StoreError,
    TokenError,
    TokenFile,
    forget_workspace,
    resolve_config_dir,
    resolve_state_dir,
)
from orbit.cli.errors import _exit_on
from orbit.cli.options import ConfigDirOption, StateDirOption


def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


def backup(
    path: Annotated[Path, typer.Argument(help="The file to write the backup to.")],
    force: Annotated[bool, typer.Option("--force", help="Replace PATH if it exists.")] = False,
    state: StateDirOption = None,
) -> None:
    """Back up the database of saved sessions to PATH, safely even while orbit runs."""
    target = path.expanduser().absolute()
    if target.exists() and not force:
        typer.echo(f"{target} already exists; pass --force to replace it.", err=True)
        raise typer.Exit(2)
    with _exit_on(StoreError):
        asyncio.run(_backed_up(resolve_state_dir(state, os.environ), target))
    typer.echo(f"Backed up orbit's database to {target}.")


async def _backed_up(state: Path, target: Path) -> None:
    """Back up the database in ``state`` to ``target``, registering and importing nothing."""
    store = await SqliteStore.open_for_backup(state)
    try:
        await store.backup(target)
    finally:
        await store.close()


def forget(
    workspace: Annotated[
        str, typer.Argument(metavar="PATH", help="The workspace's directory, or its id.")
    ],
    state: StateDirOption = None,
) -> None:
    """Delete everything orbit keeps of the workspace at PATH: its saved sessions and their
    events, its session locks and fallback files, and, for a clone orbit made, its directory."""
    state_dir = resolve_state_dir(state, os.environ)
    with _exit_on(PurgeRefused, StoreError, OSError):
        purged = asyncio.run(_forgotten(state_dir, workspace))
    typer.echo(_forgotten_note(purged))


async def _forgotten(state: Path, key: str) -> Purged:
    """Purge the workspace whose path or id is ``key`` from the store in ``state``, as
    :func:`forget_workspace` does. Raises :class:`PurgeRefused` if there's none, a running
    ``orbit serve`` serves it, another orbit holds its lock or purging it would lose work."""
    store = await SqliteStore.open(state)
    try:
        return await forget_workspace(store, key)
    finally:
        await store.close()


def _forgotten_note(purged: Purged) -> str:
    """What's said once ``purged`` is purged: what was deleted."""
    deleted = [
        _counted(purged.sessions, "session"),
        _counted(purged.fallback_files, "fallback file"),
    ]
    if purged.clone is not None:
        deleted.append(f"its clone {purged.clone}")
    *rest, last = deleted
    return f"Forgot {purged.workspace.path}: deleted {', '.join(rest)} and {last}."


def _counted(count: int, thing: str) -> str:
    """``count`` of ``thing``, made plural unless there's one."""
    return f"{count} {thing}" if count == 1 else f"{count} {thing}s"


def rotate(config_dir: ConfigDirOption = None) -> None:
    """Replace the token in your config directory with a new one; frontends connecting need
    the new token at once, while those connected stay until orbit serve restarts."""
    token_file = TokenFile(resolve_config_dir(config_dir, os.environ))
    with _exit_on(TokenError):
        token_file.rotate()
    typer.echo(
        f"Rotated the token in {token_file.path}; frontends connecting need the new token,"
        " while those connected stay until orbit serve restarts."
    )
