"""The user's own orbit configuration, ``config.toml`` in the config directory."""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.storage import StorageSettings, load_storage

CONFIG_DIR_VARIABLE = "ORBIT_CONFIG_DIR"
USER_CONFIG_FILE = "config.toml"


def resolve_config_dir(flag: Path | None, environ: Mapping[str, str]) -> Path:
    """The config directory, expanded and absolute: ``flag``, if given, else ``ORBIT_CONFIG_DIR``
    in ``environ``, else ``orbit`` in its ``XDG_CONFIG_HOME``, else ``~/.config/orbit``."""
    if flag is not None:
        chosen = flag
    elif environ.get(CONFIG_DIR_VARIABLE):
        chosen = Path(environ[CONFIG_DIR_VARIABLE])
    elif environ.get("XDG_CONFIG_HOME"):
        chosen = Path(environ["XDG_CONFIG_HOME"]) / "orbit"
    else:
        chosen = Path("~/.config/orbit")
    return chosen.expanduser().absolute()


def read_storage(config_dir: Path) -> StorageSettings:
    """The storage settings in ``config_dir``'s file over the defaults; just the defaults
    without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    path = config_dir / USER_CONFIG_FILE
    if not path.exists():
        return StorageSettings()
    try:
        return load_storage(tomllib.loads(path.read_text()))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
