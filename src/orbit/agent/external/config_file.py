"""Orbit's project configuration file, ``.orbit/config.toml``."""

from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Table

from orbit.agent.application.ports import ConfigWriteFailed
from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.config import OrbitConfig, load_config
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.toml_files import read_toml
from orbit.agent.external.whole_files import write_whole

CONFIG_PATH = ".orbit/config.toml"


def read_config(root: str) -> OrbitConfig:
    """The configuration in ``root``'s file over the defaults, whose roles are the built-in
    ones; just the defaults without a file. A role's instructions naming a Markdown file are
    that file's text (see :func:`resolve_instructions`), taken from the file's directory.

    Raises :class:`ConfigError`, naming the file, if it or a role's instructions file can't be
    read or is invalid.
    """
    built_in = read_built_in_roles()
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        return load_config({}, built_in)
    return read_toml(
        path, lambda table: load_config(_with_instructions(table, path.parent), built_in)
    )


def resolve_instructions(value: str, base: Path) -> str:
    """A role's instructions given as ``value``: the text of the UTF-8 file it names, relative
    to ``base``, if it's one line ending in ``.md``; otherwise ``value`` itself.

    Raises :class:`ConfigError`, naming the file, if it can't be read.
    """
    if "\n" in value or not value.endswith(".md"):
        return value
    path = base / value
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _with_instructions(data: dict[str, Any], base: Path) -> dict[str, Any]:
    """``data`` with each of its roles' instructions resolved from ``base``; whatever isn't a
    role's text is left for the configuration's validation to refuse."""
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return data
    resolved = {name: _role_with_instructions(name, fields, base) for name, fields in roles.items()}
    return {**data, "roles": resolved}


def _role_with_instructions(name: str, fields: Any, base: Path) -> Any:
    if not isinstance(fields, dict) or not isinstance(fields.get("instructions"), str):
        return fields
    try:
        instructions = resolve_instructions(fields["instructions"], base)
    except ConfigError as exc:
        raise ConfigError(f"roles.{name}.instructions: {exc}") from None
    return {**fields, "instructions": instructions}


def write_checks(root: str, checks: ChecksProposal) -> None:
    """Make ``checks`` the verification steps and setup in ``root``'s file, creating it if it's
    missing. The file's steps and setup are replaced; everything else in it stays as it was.

    Raises :class:`ConfigWriteFailed`, naming the file, when it can't be read, parsed or written.
    """
    path = Path(root) / CONFIG_PATH
    try:
        _write_checks(path, checks)
    except (OSError, UnicodeDecodeError, TOMLKitError) as exc:
        raise ConfigWriteFailed(f"Couldn't save the checks to {path}: {exc}") from exc


def _write_checks(path: Path, checks: ChecksProposal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    )
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
    write_whole(path, tomlkit.dumps(document))


def _steps(checks: ChecksProposal) -> AoT:
    steps = tomlkit.aot()
    for step in checks.steps:
        table: Table = tomlkit.table()
        table["name"] = step.name
        table["command"] = list(step.command)
        steps.append(table)
    steps[-1].add(tomlkit.nl())  # A blank line before whatever follows.
    return steps
