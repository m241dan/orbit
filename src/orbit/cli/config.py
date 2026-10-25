"""orbit config show: the configuration orbit resolves for a workspace, each value with the
layer it came from."""

import os
from pathlib import Path

import typer

from orbit.agent import (
    ConfigReport,
    UnknownKey,
    config_report,
    read_config_sources,
    read_server,
    read_storage,
    read_user_config,
    resolve_config_dir,
    subtree,
)
from orbit.cli.backend import _workspace
from orbit.cli.config_text import config_json, config_text
from orbit.cli.errors import _configured, _exit_on
from orbit.cli.options import (
    ConfigDirOption,
    ConfigJsonOption,
    FullOption,
    KeyArgument,
    WorkspaceOption,
)
from orbit.cli.telemetry import _load
from orbit.telemetry import TelemetrySettings


def show(
    key: KeyArgument = None,
    workspace: WorkspaceOption = Path("."),
    as_json: ConfigJsonOption = False,
    full: FullOption = False,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show the configuration orbit resolves for a workspace from this machine's files.

    Every setting in effect is listed, one per line, with the layer it came from: default
    (orbit's own, in no file), package (the roles shipped with orbit), user and the path of your
    config.toml, or project and the path of the workspace's .orbit/config.toml. That's the
    limits, each role field by field, the verification steps, the setup, and your [storage],
    [server] and [telemetry]; a KEY shows only the settings under it. Roles' instructions show as
    the file they're in and their length, or with --full their text; --json prints the same as
    one JSON object. No orbit serve needs to be running, and nothing is written. Exits with
    status 2 if the configuration is invalid, or KEY names no setting.
    """
    root = _workspace(workspace)
    user_config = resolve_config_dir(config_dir, os.environ)
    report = _configured(lambda: _report(root, user_config))
    if key is not None:
        with _exit_on(UnknownKey):
            report = subtree(report, key)
    if as_json:
        typer.echo(config_json(report, full=full))
    else:
        typer.echo(config_text(report, full=full, root=root, home=Path.home()))


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
