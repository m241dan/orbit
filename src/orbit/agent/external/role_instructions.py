"""The instructions a configuration file's roles give and the prompts its triggers give: their
text, or the Markdown files they name, read from beside the file."""

from collections.abc import Collection
from importlib.resources.abc import Traversable
from typing import Any

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.roles import names_file


def resolve_instructions(value: str, base: Traversable) -> str:
    """A role's instructions or a trigger's prompt given as ``value``: the text of the UTF-8 file
    it names, relative to ``base``, if it names one (see
    :func:`~orbit.agent.domain.roles.names_file`); otherwise ``value`` itself.

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
    data: dict[str, Any], base: Traversable, leave: Collection[str] = ()
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
        name: fields
        if name in leave
        else _with_file_text(f"roles.{name}", fields, "instructions", base)
        for name, fields in roles.items()
    }
    return {**data, "roles": resolved}


def with_prompts(data: dict[str, Any], base: Traversable) -> dict[str, Any]:
    """``data`` with each of its triggers' prompts resolved from ``base``, as a role's
    instructions are; whatever isn't a trigger's text is left for the configuration's validation
    to refuse.

    Raises :class:`ConfigError`, naming the trigger and the file, if a file can't be read.
    """
    triggers = data.get("triggers")
    if not isinstance(triggers, dict):
        return data
    resolved = {
        name: _with_file_text(f"triggers.{name}", fields, "prompt", base)
        for name, fields in triggers.items()
    }
    return {**data, "triggers": resolved}


def _with_file_text(key: str, fields: Any, field: str, base: Traversable) -> Any:
    """``fields``, the table at ``key``, with its ``field`` resolved from ``base`` (see
    :func:`resolve_instructions`) if it's text; as they are otherwise.

    Raises :class:`ConfigError`, naming the field and the file, if the file can't be read.
    """
    if not isinstance(fields, dict) or not isinstance(fields.get(field), str):
        return fields
    try:
        text = resolve_instructions(fields[field], base)
    except ConfigError as exc:
        raise ConfigError(f"{key}.{field}: {exc}") from None
    return {**fields, field: text}
