"""Orbit's project configuration file, ``.orbit/config.toml``."""

import tomllib
from pathlib import Path

from orbit.agent.domain.roles import ConfigError, OrbitConfig, load_config

CONFIG_PATH = ".orbit/config.toml"


def read_config(root: str = ".") -> OrbitConfig:
    """The configuration in ``root``'s file over the defaults; just the defaults without one.

    Raises :class:`ConfigError`, naming the file, if it's invalid.
    """
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        return load_config({})
    try:
        return load_config(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
