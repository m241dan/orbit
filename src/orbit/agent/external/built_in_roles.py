"""The built-in roles, described by the ``roles.toml`` shipped with orbit in ``instructions/``
and told the Markdown files beside it that it names."""

from importlib.resources import files
from importlib.resources.abc import Traversable

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.roles import Role, built_in_roles, load_built_in_roles
from orbit.agent.external.toml_files import read_toml

ROLE_DIRECTORY = files(__package__) / "instructions"
"""The built-in roles' directory in the package: their ``roles.toml`` and Markdown files."""


def read_built_in_roles(directory: Traversable = ROLE_DIRECTORY) -> dict[str, Role]:
    """The built-in roles ``directory``'s ``roles.toml`` describes, each told the text of the
    Markdown file there that it names.

    Raises :class:`ConfigError`, naming the file, if ``roles.toml`` or a Markdown file it names
    can't be read or is invalid.
    """
    roles = read_toml(directory / "roles.toml", load_built_in_roles)
    return built_in_roles(
        roles, {name: _read_text(directory / role.instructions) for name, role in roles.items()}
    )


def _read_text(path: Traversable) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None
