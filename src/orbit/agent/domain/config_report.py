"""What ``orbit config show`` reports of a workspace's configuration: each setting in effect, by
its dotted key, with the layer it came from."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer
from orbit.agent.domain.roles import Role, names_file
from orbit.agent.domain.verification import VerificationStep


@dataclass(frozen=True)
class Written:
    """A configuration file in ``layer`` as written: its ``data``, empty without a file."""

    layer: Layer
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigSources:
    """A workspace's configuration in effect, ``config``, with what it was resolved from: the
    layer each of its roles' fields came from, ``role_layers``, by role and field name; its
    ``project`` file and the ``user`` config as written; and the files the package's roles'
    instructions are in, ``package_files``, by role."""

    config: OrbitConfig
    role_layers: Mapping[str, Mapping[str, Layer]]
    project: Written
    user: Written
    package_files: Mapping[str, str]


@dataclass(frozen=True)
class Instructions:
    """A role's instructions in effect: their ``text``, and the ``file`` it's in as the layer
    they came from names it, ``None`` if that gives the text itself."""

    text: str
    file: str | None

    @property
    def lines(self) -> int:
        """How many lines the text has."""
        return len(self.text.splitlines())


@dataclass(frozen=True)
class Setting:
    """A setting in effect: its ``value``, and the ``layer`` it came from."""

    value: Any
    layer: Layer


type ConfigReport = dict[str, Setting]
"""The settings in effect by dotted key, such as ``roles.reviewer.model``, in order."""


class UnknownKey(LookupError):
    """No setting is at or under a key; the message names it."""


def config_report(sources: ConfigSources, tables: Mapping[str, BaseModel]) -> ConfigReport:
    """The settings in effect that ``sources`` give, each with the layer it came from: the
    limits, each role field by field, the verification steps and the setup, then each of the
    user config's ``tables``, by name, as read, field by field."""
    config, project = sources.config, sources.project
    report = _limits(config, project) | _roles(sources)
    report |= _steps(config.verification.steps, project) | _setup(config, project)
    for name, table in tables.items():
        report |= _table(name, table, sources.user)
    return report


def subtree(report: ConfigReport, key: str) -> ConfigReport:
    """The settings of ``report`` at the dotted ``key`` or under it, such as those of
    ``roles.reviewer``.

    Raises :class:`UnknownKey` naming ``key`` if there are none.
    """
    found = {name: value for name, value in report.items() if f"{name}.".startswith(f"{key}.")}
    if not found:
        raise UnknownKey(f"No setting {key} in orbit's configuration.")
    return found


def _limits(config: OrbitConfig, project: Written) -> ConfigReport:
    """``config``'s limits, each the ``project`` file's if it sets it."""
    return {
        key: Setting(getattr(config, key), _layer(key in project.data, project))
        for key in LIMIT_KEYS
    }


def _roles(sources: ConfigSources) -> ConfigReport:
    """Each role in effect, field by field, each from the layer it came from."""
    report: ConfigReport = {}
    for name in sources.config.roles:
        layers = sources.role_layers[name]
        for key in Role.model_fields:
            report[f"roles.{name}.{key}"] = Setting(_role_field(name, key, sources), layers[key])
    return report


def _role_field(name: str, key: str, sources: ConfigSources) -> Any:
    """The value of role ``name``'s field ``key``; its instructions with the file they're in."""
    role = sources.config.roles[name]
    if key != "instructions":
        return getattr(role, key)
    given = _given_instructions(name, sources.role_layers[name][key], sources)
    return Instructions(role.instructions, given if names_file(given) else None)


def _given_instructions(name: str, layer: Layer, sources: ConfigSources) -> str:
    """Role ``name``'s instructions as ``layer``, the one they came from, gives them."""
    if layer == PACKAGE:
        return sources.package_files[name]
    written = sources.user if layer.name == "user" else sources.project
    given: str = written.data["roles"][name]["instructions"]
    return given


def _steps(steps: tuple[VerificationStep, ...], project: Written) -> ConfigReport:
    """The verification ``steps``, numbered from 1, field by field, each from the ``project``
    file if it gives them; no steps as one setting."""
    layer = _layer(_sets(project.data, "verification", "steps"), project)
    if not steps:
        return {"verification.steps": Setting(steps, layer)}
    report: ConfigReport = {}
    for number, step in enumerate(steps, start=1):
        report[f"verification.steps.{number}.name"] = Setting(step.name, layer)
        report[f"verification.steps.{number}.command"] = Setting(step.command, layer)
    return report


def _setup(config: OrbitConfig, project: Written) -> ConfigReport:
    """``config``'s setup, field by field, each from the ``project`` file if it sets it; no
    setup as one setting, as only a file gives one."""
    if config.setup is None:
        return {"setup": Setting(None, DEFAULT)}
    return _table("setup", config.setup, project)


def _table(name: str, table: BaseModel, written: Written) -> ConfigReport:
    """``table``, the one ``written`` calls ``name``, field by field, each from ``written``'s
    layer if it sets it."""
    return {
        f"{name}.{key}": Setting(
            getattr(table, key), _layer(_sets(written.data, name, key), written)
        )
        for key in type(table).model_fields
    }


def _sets(data: Mapping[str, Any], table: str, key: str) -> bool:
    """Whether ``data`` sets ``key`` in its ``table``."""
    given = data.get(table)
    return isinstance(given, Mapping) and key in given


def _layer(given: bool, written: Written) -> Layer:
    """``written``'s layer if the setting is ``given`` there, else the default."""
    return written.layer if given else DEFAULT
