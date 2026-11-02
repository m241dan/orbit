"""What ``orbit config show`` reports of a workspace's configuration: each setting in effect, by
its dotted key, with the layer it came from, or its skill map, saying which agents get each skill
and from where."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from orbit.agent.domain.checks import VerificationSet
from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer
from orbit.agent.domain.roles import Role, names_file
from orbit.agent.domain.skills import SkillLibrary, SkillMapping
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import REVIEW_ROLES

ROLE_CELL = "role"
TASK_CELL = "task"
REVIEWED_TASK_CELL = "reviewed task"
MAPPED_CELL = "mapped"
NEVER_CELL = "—"
"""What a skill map's cell says of a role's agents and a skill: they get it from the role's
skills, from their task's list, from the reviewed task's list, from the ``[[skills]]`` entries,
or never."""


@dataclass(frozen=True)
class Written:
    """A configuration file in ``layer`` as written: its ``data``, empty without a file."""

    layer: Layer
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigSources:
    """A workspace's configuration in effect, ``config``, with what it was resolved from: the
    layer each of its roles' fields came from, ``role_layers``, by role and field name; its
    ``project`` file and the ``user`` config as written; the files the package's roles'
    instructions are in, ``package_files``, by role; the layer each of its triggers' fields came
    from, ``trigger_layers``, by trigger and field name; and the files the package's triggers'
    prompts are in, ``template_files``, by trigger, for those that name one."""

    config: OrbitConfig
    role_layers: Mapping[str, Mapping[str, Layer]]
    project: Written
    user: Written
    package_files: Mapping[str, str]
    trigger_layers: Mapping[str, Mapping[str, Layer]] = field(default_factory=dict)
    template_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Instructions:
    """A role's instructions or a trigger's prompt in effect: their ``text``, and the ``file``
    it's in as the layer they came from names it, ``None`` if that gives the text itself."""

    text: str
    file: str | None

    @property
    def lines(self) -> int:
        """How many lines the text has."""
        return len(self.text.splitlines())


@dataclass(frozen=True)
class SkillFileText:
    """A skill file in effect: its ``text``."""

    text: str

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


@dataclass(frozen=True)
class SkillMap:
    """Which agents get each skill and from where: the ``roles``, by name, in the
    configuration's order, and the ``cells`` of each skill in the library, in name order, each
    role's by its name, in the same order, as :func:`skill_cell` gives it."""

    roles: tuple[str, ...]
    cells: Mapping[str, Mapping[str, str]]


class UnknownKey(LookupError):
    """No setting is at or under a key; the message names it."""


def config_report(sources: ConfigSources, tables: Mapping[str, BaseModel]) -> ConfigReport:
    """The settings in effect that ``sources`` give, each with the layer it came from: the
    limits, each role field by field, each trigger field by field, the verification steps and
    their sets' tables, the setup, the skills by path and every skill file, then each of the user
    config's ``tables``, by name, as read, field by field."""
    config, project = sources.config, sources.project
    report = _limits(config, project) | _roles(sources) | _triggers(sources)
    report |= _steps(config.verification.steps, project)
    report |= _verification_sets(config.verification.sets, project) | _setup(config, project)
    report |= _skills(config.skills, project) | _skill_files(config.skill_library)
    for name, table in tables.items():
        report |= _table(name, table, sources.user)
    return report


def subtree(report: ConfigReport, key: str) -> ConfigReport:
    """The settings of ``report`` at the dotted ``key`` or under it, such as those of
    ``roles.reviewer``, as :func:`key_parts` splits each, so a skill file's key matches only by
    its whole path.

    Raises :class:`UnknownKey` naming ``key`` if there are none.
    """
    parts = key_parts(key)
    found = {
        name: value for name, value in report.items() if key_parts(name)[: len(parts)] == parts
    }
    if not found:
        raise UnknownKey(f"No setting {key} in orbit's configuration.")
    return found


def key_parts(key: str) -> list[str]:
    """The parts of the dotted ``key``: those between its dots, but a skill file's section,
    skill and path, which may have dots of its own."""
    if key.startswith("skill_files."):
        return key.split(".", 2)
    return key.split(".")


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


def _triggers(sources: ConfigSources) -> ConfigReport:
    """Each trigger in effect, field by field, each from the layer it came from."""
    return {
        f"triggers.{name}.{key}": Setting(
            _trigger_field(name, key, sources), sources.trigger_layers[name][key]
        )
        for name in sources.config.triggers
        for key in Trigger.model_fields
    }


def _trigger_field(name: str, key: str, sources: ConfigSources) -> Any:
    """The value of trigger ``name``'s field ``key``; its prompt, if any, with the file it's
    in."""
    value = getattr(sources.config.triggers[name], key)
    if key != "prompt" or value is None:
        return value
    given = _given_prompt(name, sources.trigger_layers[name][key], sources)
    return Instructions(value, given if names_file(given) else None)


def _given_prompt(name: str, layer: Layer, sources: ConfigSources) -> str:
    """Trigger ``name``'s prompt as ``layer``, the one it came from, gives it: the package's
    template file, if it names one, or the project file's value."""
    if layer == PACKAGE:
        return sources.template_files.get(name, "")
    given: str = sources.project.data["triggers"][name]["prompt"]
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
        report[f"verification.steps.{number}.set"] = Setting(step.set, layer)
    return report


def _verification_sets(sets: Mapping[str, VerificationSet], project: Written) -> ConfigReport:
    """The verification ``sets``' tables, by name, field by field, each from the ``project``
    file if it sets it; no tables as one setting."""
    verification = project.data.get("verification", {})
    if not sets:
        return {"verification.sets": Setting({}, _layer("sets" in verification, project))}
    given = verification["sets"]
    return {
        f"verification.sets.{name}.{key}": Setting(
            getattr(table, key), _layer(_sets(given, name, key), project)
        )
        for name, table in sets.items()
        for key in VerificationSet.model_fields
    }


def _setup(config: OrbitConfig, project: Written) -> ConfigReport:
    """``config``'s setup, field by field, each from the ``project`` file if it sets it; no
    setup as one setting, as only a file gives one."""
    if config.setup is None:
        return {"setup": Setting(None, DEFAULT)}
    return _table("setup", config.setup, project)


def _skills(mappings: tuple[SkillMapping, ...], project: Written) -> ConfigReport:
    """The skills by path, ``mappings``, numbered from 1, field by field, each from the
    ``project`` file if it gives them; no mappings as one setting."""
    layer = _layer("skills" in project.data, project)
    if not mappings:
        return {"skills": Setting(mappings, layer)}
    report: ConfigReport = {}
    for number, mapping in enumerate(mappings, start=1):
        report[f"skills.{number}.paths"] = Setting(mapping.paths, layer)
        report[f"skills.{number}.use"] = Setting(mapping.use, layer)
    return report


def _skill_files(library: SkillLibrary) -> ConfigReport:
    """Every file of the ``library``'s skills, skill by skill and file by file in order of their
    names and paths, by ``skill_files``, the skill's name and the file's path, each from its own
    layer."""
    return {
        f"skill_files.{name}.{path}": Setting(SkillFileText(file.text), file.layer)
        for name, skill in library.skills.items()
        for path, file in sorted(skill.files.items())
    }


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


def skill_map(config: OrbitConfig) -> SkillMap:
    """``config``'s skill map: a cell for each skill of its library and each of its roles, as
    :func:`skill_cell` gives it from its ``[[skills]]`` entries."""
    mapped = {name for mapping in config.skills for name in mapping.use}
    cells = {
        skill: {
            name: skill_cell(name, role, skill, mapped=skill in mapped)
            for name, role in config.roles.items()
        }
        for skill in config.skill_library.skills
    }
    return SkillMap(tuple(config.roles), cells)


def skill_cell(name: str, role: Role, skill: str, *, mapped: bool) -> str:
    """Where the agents of ``role``, called ``name``, get ``skill``, which the ``[[skills]]``
    entries use if ``mapped``: from its skills if they name it; else, for a conversational role,
    which has no task's list, from the entries, or never; and for the other roles as
    :func:`_task_cell` says."""
    if skill in role.skills:
        return ROLE_CELL
    if role.conversation:
        return MAPPED_CELL if mapped else NEVER_CELL
    return _task_cell(name, mapped=mapped)


def _task_cell(name: str, *, mapped: bool) -> str:
    """Where the agents of the task role ``name`` get a skill its skills don't name, which the
    ``[[skills]]`` entries use if ``mapped``: from their task's list, a review role's the reviewed
    task's, or the entries when it has none."""
    task = REVIEWED_TASK_CELL if name in REVIEW_ROLES else TASK_CELL
    return f"{task} / {MAPPED_CELL}" if mapped else task
