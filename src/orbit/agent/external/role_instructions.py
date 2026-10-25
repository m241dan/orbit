"""The instructions a configuration file's roles give: their text, or the Markdown files they
name, read from beside the file."""

from collections.abc import Collection
from pathlib import Path
from typing import Any

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.roles import names_file


def resolve_instructions(value: str, base: Path) -> str:
    """A role's instructions given as ``value``: the text of the UTF-8 file it names, relative
    to ``base``, if it names one (see :func:`~orbit.agent.domain.roles.names_file`); otherwise
    ``value`` itself.

    Raises :class:`ConfigError`, naming the file, if it can't be read.
    """
    if not names_file(value):
        return value
    path = base / value
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def with_instructions(
    data: dict[str, Any], base: Path, leave: Collection[str] = ()
) -> dict[str, Any]:
    """``data`` with each of its roles' instructions resolved from ``base``, but those of the
    roles named in ``leave``; whatever isn't a role's text is left for the configuration's
    validation to refuse.

    Raises :class:`ConfigError`, naming the role and the file, if a file can't be read.
    """
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return data
    resolved = {
        name: fields if name in leave else _role_with_instructions(name, fields, base)
        for name, fields in roles.items()
    }
    return {**data, "roles": resolved}


def _role_with_instructions(name: str, fields: Any, base: Path) -> Any:
    if not isinstance(fields, dict) or not isinstance(fields.get("instructions"), str):
        return fields
    try:
        instructions = resolve_instructions(fields["instructions"], base)
    except ConfigError as exc:
        raise ConfigError(f"roles.{name}.instructions: {exc}") from None
    return {**fields, "instructions": instructions}
