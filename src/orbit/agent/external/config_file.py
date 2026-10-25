"""Orbit's project configuration file, ``.orbit/config.toml``."""

from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Table

from orbit.agent.application.ports import ConfigWriteFailed
from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.config import OrbitConfig, load_config
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PersonalRoleError
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.role_instructions import with_instructions
from orbit.agent.external.toml_files import read_toml
from orbit.agent.external.user_config import read_user_roles
from orbit.agent.external.whole_files import write_whole

CONFIG_PATH = ".orbit/config.toml"


def read_config(root: str, config_dir: Path) -> OrbitConfig:
    """The configuration in ``root``'s file over the role fields and personal roles the user
    config in ``config_dir`` sets (see :func:`read_user_roles`) and the defaults, whose roles
    are the built-in ones; without a file, the defaults with the user config's roles. A role's
    instructions naming a Markdown file are that file's text (see
    :func:`~orbit.agent.external.role_instructions.resolve_instructions`), taken from the file's
    directory.

    Raises :class:`ConfigError`, naming the file, if it, the user config or a role's
    instructions file can't be read or is invalid; it names the user config if the personal
    roles break a rule across the roles that the others keep.
    """
    built_in = read_built_in_roles()
    user = read_user_roles(config_dir, built_in)
    path = Path(root) / CONFIG_PATH
    exists = path.exists()
    data = read_toml(path, lambda table: with_instructions(table, path.parent)) if exists else {}
    try:
        return load_config(data, built_in, user, str(path) if exists else None)
    except PersonalRoleError as exc:
        raise ConfigError(f"{user.layer.path}: {exc}") from None
    except ConfigError as exc:  # Without a file, only the personal roles can be at fault.
        raise ConfigError(f"{path}: {exc}") from None


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
