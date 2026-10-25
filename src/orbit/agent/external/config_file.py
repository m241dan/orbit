"""Orbit's project configuration file, ``.orbit/config.toml``."""

from importlib.resources import files
from pathlib import Path

from orbit.agent.application.ports import ConfigWriteFailed
from orbit.agent.domain.checks import ChecksProposal
from orbit.agent.domain.config import LayeredConfig, OrbitConfig, load_layered
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_report import ConfigSources, Written
from orbit.agent.domain.config_update import reference_with_checks, with_checks
from orbit.agent.domain.layers import Layer, PersonalRoleError
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.agent.external.role_instructions import with_instructions
from orbit.agent.external.toml_files import read_toml
from orbit.agent.external.user_config import read_user_config, read_user_roles, user_layer
from orbit.agent.external.whole_files import write_whole

CONFIG_PATH = ".orbit/config.toml"

REFERENCE = files(__package__) / "reference" / "config.toml"
"""The reference config shipped with orbit, as
:func:`~orbit.agent.external.reference_config.reference_config` generates it."""


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
    return _read_layered(root, config_dir).config


def read_config_sources(root: str, config_dir: Path) -> ConfigSources:
    """What the configuration in effect in ``root`` is resolved from: the configuration
    :func:`read_config` reads, with the layer each of its roles' fields came from, ``root``'s
    file and the user config in ``config_dir`` as written, and the files the built-in roles'
    instructions are in.

    Raises :class:`ConfigError` as :func:`read_config` does.
    """
    layered = _read_layered(root, config_dir)
    path = Path(root) / CONFIG_PATH
    project = read_toml(path, dict) if path.exists() else {}
    return ConfigSources(
        layered.config,
        layered.role_layers,
        Written(Layer("project", str(path)), project),
        Written(user_layer(config_dir), read_user_config(config_dir, dict, {})),
        read_instruction_files(),
    )


def _read_layered(root: str, config_dir: Path) -> LayeredConfig:
    """The configuration :func:`read_config` reads, with the layer each of its roles' fields
    came from.

    Raises :class:`ConfigError` as :func:`read_config` does.
    """
    built_in = read_built_in_roles()
    user = read_user_roles(config_dir, built_in)
    path = Path(root) / CONFIG_PATH
    exists = path.exists()
    data = read_toml(path, lambda table: with_instructions(table, path.parent)) if exists else {}
    try:
        return load_layered(data, built_in, user, str(path) if exists else None)
    except PersonalRoleError as exc:
        raise ConfigError(f"{user.layer.path}: {exc}") from None
    except ConfigError as exc:  # Without a file, only the personal roles can be at fault.
        raise ConfigError(f"{path}: {exc}") from None


def write_checks(root: str, checks: ChecksProposal) -> None:
    """Make ``checks`` the verification steps and setup in ``root``'s file, replacing its steps
    and setup and leaving its other lines as they were, as
    :func:`~orbit.agent.domain.config_update.with_checks` does. Without a file, write the
    reference config there, making ``.orbit/`` if it's missing, with ``checks`` in place of its
    examples of steps and setup, as
    :func:`~orbit.agent.domain.config_update.reference_with_checks` puts them.

    Raises :class:`ConfigWriteFailed`, naming the file, when it can't be read, parsed or written.
    """
    path = Path(root) / CONFIG_PATH
    try:
        _write_checks(path, checks)
    except (OSError, UnicodeDecodeError, ConfigError) as exc:
        raise ConfigWriteFailed(f"Couldn't save the checks to {path}: {exc}") from exc


def _write_checks(path: Path, checks: ChecksProposal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = with_checks(path.read_text(encoding="utf-8"), checks)
    else:
        text = reference_with_checks(REFERENCE.read_text(encoding="utf-8"), checks)
    write_whole(path, text)
