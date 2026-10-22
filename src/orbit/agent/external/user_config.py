"""The user's own orbit configuration, ``config.toml`` in the config directory."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orbit.agent.domain.remotes import REMOTE_PATHS, RemoteSettings, load_remotes
from orbit.agent.domain.server import SERVER_PATHS, ServerSettings, load_server
from orbit.agent.domain.storage import StorageSettings, load_storage
from orbit.agent.external.toml_files import read_toml

USER_CONFIG_FILE = "config.toml"


def read_storage(config_dir: Path) -> StorageSettings:
    """The storage settings in ``config_dir``'s file over the defaults; just the defaults
    without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    return _read(config_dir, load_storage, StorageSettings())


def read_server(config_dir: Path) -> ServerSettings:
    """The server settings in ``config_dir``'s file over the defaults, their paths made
    absolute: ``~`` expanded, and a relative one taken from ``config_dir``; just the defaults
    without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    settings = _read(config_dir, load_server, ServerSettings())
    return _resolved(settings, SERVER_PATHS, config_dir)


def read_remotes(config_dir: Path) -> dict[str, RemoteSettings]:
    """The remotes in ``config_dir``'s file by name, their paths made absolute: ``~`` expanded,
    and a relative one taken from ``config_dir``; none without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    remotes: dict[str, RemoteSettings] = _read(config_dir, load_remotes, {})
    return {name: _resolved(remote, REMOTE_PATHS, config_dir) for name, remote in remotes.items()}


def _resolved[M: BaseModel](settings: M, keys: tuple[str, ...], config_dir: Path) -> M:
    """``settings`` with the path each of its ``keys`` gives taken from ``config_dir`` after
    expanding ``~``."""
    given = {key: getattr(settings, key) for key in keys}
    paths = {key: str(config_dir / Path(path).expanduser()) for key, path in given.items() if path}
    return settings.model_copy(update=paths)


def _read[T](config_dir: Path, load: Callable[[dict[str, Any]], T], default: T) -> T:
    """What ``load`` makes of ``config_dir``'s file; ``default`` without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    path = config_dir / USER_CONFIG_FILE
    if not path.exists():
        return default
    return read_toml(path, load)
