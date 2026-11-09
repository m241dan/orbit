"""Commented-out TOML, as orbit's reference configs write it: each setting under a line saying
what it does, a setting with no value written as unset."""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import tomlkit
from pydantic import BaseModel, TypeAdapter

from orbit.agent.domain.config_update import FOR_EXAMPLE
from orbit.agent.domain.verification import VerificationStep

type SettingLine = Callable[[str, Any], str]
"""The commented-out line giving a key, the first argument, its value, the second."""


def field_descriptions(model: type[Any]) -> dict[str, str]:
    """The description of each of ``model``'s fields that has one, by name, in order."""
    properties: dict[str, dict[str, Any]] = TypeAdapter(model).json_schema()["properties"]
    return {
        key: field["description"] for key, field in properties.items() if "description" in field
    }


def comments(lines: Iterable[str]) -> list[str]:
    """``lines``, each commented out."""
    return [f"# {line}" for line in lines]


def key_line(key: str, value: Any) -> str:
    """The commented-out line giving ``key`` its ``value``, as TOML, a table inline."""
    return f"# {key} = {_inline(value).as_string()}"


def setting_line(key: str, value: Any) -> str:
    """The commented-out line giving ``key`` its ``value``, as :func:`key_line` writes it; unset,
    as TOML has no value for that, if it's ``None``."""
    return f"# {key}: unset" if value is None else key_line(key, value)


def _inline(value: Any) -> Any:
    """``value`` as a TOML item, a table as an inline one, as a key's value is on its line."""
    if not isinstance(value, dict):
        return tomlkit.item(value)
    table = tomlkit.inline_table()
    table.update(value)
    return table


def described(
    values: Mapping[str, Any],
    descriptions: Mapping[str, str],
    line: SettingLine = setting_line,
) -> list[str]:
    """Each of ``values``, by key, commented out under its line of ``descriptions``, its own
    line written by ``line``, by default :func:`setting_line`."""
    return [
        text
        for key, value in values.items()
        for text in (f"# {descriptions[key]}", line(key, value))
    ]


def example_table(
    description: str,
    header: str,
    example: BaseModel | VerificationStep,
    descriptions: Mapping[str, str] | None = None,
) -> list[str]:
    """The table under ``header`` that sets the fields ``example`` does, under ``description``,
    each field under its line of ``descriptions``, by default its model's own."""
    values = TypeAdapter(type(example)).dump_python(example, mode="json", exclude_unset=True)
    return [
        f"# {description} {FOR_EXAMPLE}",
        f"# {header}",
        *described(values, descriptions or field_descriptions(type(example))),
    ]
