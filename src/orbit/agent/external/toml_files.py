"""TOML files read for their settings."""

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orbit.agent.domain.config_errors import ConfigError


def read_toml[T](path: Path, load: Callable[[dict[str, Any]], T]) -> T:
    """What ``load`` makes of the UTF-8 TOML file ``path``.

    Raises :class:`ConfigError`, naming the file, if it can't be read or is invalid.
    """
    try:
        return load(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
