"""orbit config show: the configuration orbit resolves for a workspace, each value with the
layer it came from, or with --skills its skill map."""

import os
from pathlib import Path

import typer

from orbit.agent import (
    ConfigReport,
    UnknownKey,
    config_report,
    read_config,
    read_config_sources,
    read_server,
    read_storage,
    read_user_config,
    resolve_config_dir,
    skill_map,
    subtree,
)
from orbit.cli.backend import _workspace
from orbit.cli.config_text import config_json, config_text, skill_map_json, skill_map_text
from orbit.cli.errors import _configured, _exit_on
from orbit.cli.options import (
    ConfigDirOption,
    ConfigJsonOption,
    FullOption,
    KeyArgument,
    SkillsOption,
    WorkspaceOption,
)
from orbit.cli.telemetry import _load
from orbit.telemetry import TelemetrySettings

SKILLS_ALONE = "--skills shows the whole map; it takes no key or --full."
"""Why ``--skills`` with a key or ``--full`` is refused."""


def show(
    key: KeyArgument = None,
    workspace: WorkspaceOption = Path("."),
    as_json: ConfigJsonOption = False,
    full: FullOption = False,
    skills: SkillsOption = False,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show the configuration orbit resolves for a workspace from this machine's files.

    Every setting in effect is listed, one per line, with the layer it came from: default
    (orbit's own, in no file), package (the roles and skills shipped with orbit), user and the
    path of your config.toml, or project and the path of the workspace's .orbit/config.toml or
    skill file. That's the limits, each role field by field, the verification steps, the setup,
    the [[skills]] entries, every skill file as skill_files.<skill>.<path>, and your [storage],
    [server] and [telemetry]; a KEY shows only the settings under it. Roles' instructions show as
    the file they're in and their length, skill files as their length, or either with --full
    their text; --json prints the same as one JSON object. --skills prints the skill map
    instead: a row for each skill and a column for each role, each cell saying where that role's
    agents get the skill, role, task, reviewed task, mapped or —, then a line saying what each
    means; with --json, one JSON object. No orbit serve needs to be running, and nothing is
    written. Exits with status 2 if the configuration is invalid, KEY names no setting, or
    --skills is given with KEY or --full.
    """
    # #lizard forgives(parameter_count): Typer takes each of the command's options as a parameter.
    if skills:
        _refuse_with_skills(key, full)
    root = _workspace(workspace)
    user_config = resolve_config_dir(config_dir, os.environ)
    if skills:
        _show_skills(root, user_config, as_json)
        return
    report = _configured(lambda: _report(root, user_config))
    if key is not None:
        with _exit_on(UnknownKey):
            report = subtree(report, key)
    if as_json:
        typer.echo(config_json(report, full=full))
    else:
        typer.echo(config_text(report, full=full, root=root, home=Path.home()))


def _refuse_with_skills(key: str | None, full: bool) -> None:
    """Exit with status 2 if ``key`` or ``full`` is given with --skills, which shows the whole
    map."""
    if key is not None or full:
        typer.echo(SKILLS_ALONE, err=True)
        raise typer.Exit(2)


def _show_skills(root: Path, config_dir: Path, as_json: bool) -> None:
    """Print the skill map of the workspace at ``root``, over the user config in
    ``config_dir``, as a table, or with ``as_json`` as one JSON object.

    Exits with status 2 if the configuration is invalid.
    """
    skills = _configured(lambda: skill_map(read_config(str(root), config_dir)))
    typer.echo(skill_map_json(skills) if as_json else skill_map_text(skills))


def _report(root: Path, config_dir: Path) -> ConfigReport:
    """The configuration in effect in the workspace at ``root``, over the user config in
    ``config_dir``, each setting with the layer it came from.

    Raises :class:`ConfigError`, naming the file, if one is invalid.
    """
    sources = read_config_sources(str(root), config_dir)
    tables = {
        "storage": read_storage(config_dir),
        "server": read_server(config_dir),
        "telemetry": read_user_config(config_dir, _load, TelemetrySettings()),
    }
    return config_report(sources, tables)
