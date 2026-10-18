"""The user's own orbit configuration, ``config.toml`` in the config directory."""

import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.server import ServerSettings, load_server
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
    return _read(config_dir, load_storage, StorageSettings())


def read_server(config_dir: Path) -> ServerSettings:
    """The server settings in ``config_dir``'s file over the defaults; just the defaults
    without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    return _read(config_dir, load_server, ServerSettings())


def _read[T](config_dir: Path, load: Callable[[dict[str, Any]], T], default: T) -> T:
    """What ``load`` makes of ``config_dir``'s file; ``default`` without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    path = config_dir / USER_CONFIG_FILE
    if not path.exists():
        return default
    try:
        return load(tomllib.loads(path.read_text()))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
