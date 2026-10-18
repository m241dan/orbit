"""Orbit's project configuration file, ``.orbit/config.toml``."""

import tomllib
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Table

from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.config import OrbitConfig, load_config
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.external.built_in_roles import read_built_in_roles

CONFIG_PATH = ".orbit/config.toml"


def read_config(root: str = ".") -> OrbitConfig:
    """The configuration in ``root``'s file over the defaults, whose roles are the built-in
    ones; just the defaults without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    built_in = read_built_in_roles()
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        return load_config({}, built_in)
    try:
        return load_config(tomllib.loads(path.read_text()), built_in)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def write_checks(root: str, checks: ChecksProposal) -> None:
    """Make ``checks`` the verification steps and setup in ``root``'s file, creating it if it's
    missing. The file's steps and setup are replaced; everything else in it stays as it was.

    Raises ``RuntimeError``, naming the file, when it can't be read, parsed or written.
    """
    path = Path(root) / CONFIG_PATH
    try:
        _write_checks(path, checks)
    except (OSError, UnicodeDecodeError, TOMLKitError) as exc:
        raise RuntimeError(f"Couldn't save the checks to {path}: {exc}") from exc


def _write_checks(path: Path, checks: ChecksProposal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    verification = tomlkit.table(is_super_table=True)
    verification["steps"] = _steps(checks)
    document["verification"] = verification
    if checks.setup is None:
        document.pop("setup", None)
    else:
        setup = tomlkit.table()
        setup["command"] = list(checks.setup.command)
        if checks.setup.watch:
            setup["watch"] = list(checks.setup.watch)
        document["setup"] = setup
    path.write_text(tomlkit.dumps(document))


def _steps(checks: ChecksProposal) -> AoT:
    steps = tomlkit.aot()
    for step in checks.steps:
        table: Table = tomlkit.table()
        table["name"] = step.name
        table["command"] = list(step.command)
        steps.append(table)
    steps[-1].add(tomlkit.nl())  # A blank line before whatever follows.
    return steps
