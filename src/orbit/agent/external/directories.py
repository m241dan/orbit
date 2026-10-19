"""Where orbit keeps its files outside the repository: the state and config directories."""

from collections.abc import Mapping
from pathlib import Path

STATE_DIR_VARIABLE = "ORBIT_STATE_DIR"
CONFIG_DIR_VARIABLE = "ORBIT_CONFIG_DIR"


def resolve_state_dir(flag: Path | None, environ: Mapping[str, str]) -> Path:
    """The state directory, expanded and absolute: ``flag``, if given, else ``ORBIT_STATE_DIR``
    in ``environ``, else ``orbit`` in its ``XDG_STATE_HOME``, else ``~/.local/state/orbit``."""
    return _resolve(flag, environ, STATE_DIR_VARIABLE, "XDG_STATE_HOME", "~/.local/state/orbit")


def resolve_config_dir(flag: Path | None, environ: Mapping[str, str]) -> Path:
    """The config directory, expanded and absolute: ``flag``, if given, else ``ORBIT_CONFIG_DIR``
    in ``environ``, else ``orbit`` in its ``XDG_CONFIG_HOME``, else ``~/.config/orbit``."""
    return _resolve(flag, environ, CONFIG_DIR_VARIABLE, "XDG_CONFIG_HOME", "~/.config/orbit")


def _resolve(
    flag: Path | None, environ: Mapping[str, str], variable: str, xdg_home: str, default: str
) -> Path:
    """The directory, expanded and absolute: ``flag``, if given, else ``variable`` in
    ``environ``, else ``orbit`` in its ``xdg_home``, else ``default``; empty variables are
    ignored."""
    if flag is not None:
        chosen = flag
    elif environ.get(variable):
        chosen = Path(environ[variable])
    elif environ.get(xdg_home):
        chosen = Path(environ[xdg_home]) / "orbit"
    else:
        chosen = Path(default)
    return chosen.expanduser().absolute()
