"""The user's own orbit configuration, ``config.toml`` in the config directory."""

from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import Layer, UserRoles, load_user_roles
from orbit.agent.domain.remotes import REMOTE_PATHS, RemoteSettings, load_remotes
from orbit.agent.domain.server import SERVER_PATHS, ServerSettings, load_server
from orbit.agent.domain.storage import StorageSettings, load_storage
from orbit.agent.external.role_instructions import with_instructions
from orbit.agent.external.toml_files import read_toml

USER_CONFIG_FILE = "config.toml"


def read_storage(config_dir: Path) -> StorageSettings:
    """The storage settings in ``config_dir``'s file over the defaults; just the defaults
    without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    return read_user_config(config_dir, load_storage, StorageSettings())


def read_server(config_dir: Path) -> ServerSettings:
    """The server settings in ``config_dir``'s file over the defaults, their paths made
    absolute: ``~`` expanded, and a relative one taken from ``config_dir``, and the browsable
    root with its symlinks followed; just the defaults without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid, as it is
    when the browsable root isn't a directory.
    """
    settings = read_user_config(config_dir, load_server, ServerSettings())
    return _browsable(_resolved(settings, SERVER_PATHS, config_dir), config_dir)


def _browsable(settings: ServerSettings, config_dir: Path) -> ServerSettings:
    """``settings`` with their browsable root, if any, with its symlinks followed.

    Raises :class:`ConfigError`, naming ``config_dir``'s file, unless it's an absolute path to
    a directory.
    """
    given = settings.browse_root
    if given is None:
        return settings
    root = Path(given).resolve()
    if not Path(given).is_absolute() or not root.is_dir():
        problem = f"server.browse_root: {given} isn't an absolute path to a directory"
        raise ConfigError(f"{config_dir / USER_CONFIG_FILE}: {problem}")
    return settings.model_copy(update={"browse_root": str(root)})


def read_remotes(config_dir: Path) -> dict[str, RemoteSettings]:
    """The remotes in ``config_dir``'s file by name, their paths made absolute: ``~`` expanded,
    and a relative one taken from ``config_dir``; none without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    remotes: dict[str, RemoteSettings] = read_user_config(config_dir, load_remotes, {})
    return {name: _resolved(remote, REMOTE_PATHS, config_dir) for name, remote in remotes.items()}


def read_user_roles(config_dir: Path, built_in: Collection[str]) -> UserRoles:
    """The role fields ``config_dir``'s file sets, those bound to the machine, and the personal
    roles it defines beside the ``built_in`` ones, as its layer; none without a file. A
    personal role's instructions naming a Markdown file are that file's text, taken from
    ``config_dir``.

    Raises :class:`ConfigError`, naming the file, if it or a personal role's instructions file
    can't be read or is invalid, as it is when it sets a built-in role's field other than its
    model and effort.
    """
    layer = Layer("user", str(config_dir / USER_CONFIG_FILE))

    def load(data: dict[str, Any]) -> UserRoles:
        return load_user_roles(with_instructions(data, config_dir, built_in), layer, built_in)

    return read_user_config(config_dir, load, UserRoles(layer))


def _resolved[M: BaseModel](settings: M, keys: tuple[str, ...], config_dir: Path) -> M:
    """``settings`` with the path each of its ``keys`` gives taken from ``config_dir`` after
    expanding ``~``."""
    given = {key: getattr(settings, key) for key in keys}
    paths = {key: str(config_dir / Path(path).expanduser()) for key, path in given.items() if path}
    return settings.model_copy(update=paths)


def read_user_config[T](config_dir: Path, load: Callable[[dict[str, Any]], T], default: T) -> T:
    """What ``load`` makes of ``config_dir``'s file; ``default`` without a file.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    path = config_dir / USER_CONFIG_FILE
    if not path.exists():
        return default
    return read_toml(path, load)
