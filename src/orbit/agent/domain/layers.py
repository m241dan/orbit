"""The layers orbit's configuration is resolved from, and the roles resolved across them.

From least to most specific: orbit's built-in defaults, in no file; the package's roles; the
user config, which sets the role fields bound to the machine, its model, effort and runtime, for
every role or for one, and defines whole personal roles of its own; and the project file. A
role's field is the most specific layer's value, but a project role replaces a personal role of
its name entirely. A run can give the roles a runtime of its own, over the user config's for
every role but under any role's own.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import ErrorDetails

from orbit.agent.domain.config_errors import ConfigError, dotted, validation_problems
from orbit.agent.domain.roles import (
    Role,
    RoleName,
    check_runtimes,
    role_clashes,
    with_enabled_consults,
)
from orbit.agent.domain.runtimes import UNCHECKED, RuntimeRules, knows_runtime, unknown_runtime
from orbit.protocol import Effort

LayerName = Literal["default", "package", "user", "project"]

BELONGS_IN_PROJECT = (
    "belongs in the project's .orbit/config.toml; the user config sets only a role's model,"
    " effort and runtime"
)
"""Why the user config can't set a role's field other than its model, effort and runtime."""


@dataclass(frozen=True)
class Layer:
    """A layer a setting comes from, by ``name``, with the ``path`` of its file, if any: the
    user config's and the project file's have one."""

    name: LayerName
    path: str | None = None

    def __str__(self) -> str:
        return self.name if self.path is None else f"{self.name} {self.path}"


DEFAULT = Layer("default")
"""Orbit's built-in defaults, in no file."""

PACKAGE = Layer("package")
"""The roles shipped with orbit."""


class RoleFields(BaseModel):
    """The fields of a role the user config sets, those bound to the machine: its ``model``,
    ``effort`` and ``runtime``, each ``None`` if it doesn't set it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None
    effort: Effort | None = None
    runtime: str | None = None


MACHINE_FIELDS = frozenset(RoleFields.model_fields)
"""The role fields bound to the machine, which the user config sets for any role."""

MERGING_NEEDS = frozenset({"verify", "workspace"})
"""The role fields whether a role can merge depends on."""


@dataclass(frozen=True)
class UserRoles:
    """The role fields the user config in ``layer`` sets: those for ``every`` role, and those
    for each role it ``named``; and the ``personal`` roles it defines whole, by name."""

    layer: Layer
    every: RoleFields = field(default_factory=RoleFields)
    named: Mapping[str, RoleFields] = field(default_factory=dict)
    personal: Mapping[str, Role] = field(default_factory=dict)


NO_USER_ROLES = UserRoles(Layer("user"))
"""The role fields of a user config that sets none."""


@dataclass(frozen=True)
class ProjectRoles:
    """The ``roles`` the project file in ``layer`` sets, its tables by role name as it gives
    them."""

    layer: Layer
    roles: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedRoles:
    """The ``roles`` resolved across the layers, and the ``layers`` each of their fields came
    from, by role and field name; the names of the roles that are ``personal``, and of the
    personal roles project roles ``replaced``, in order."""

    roles: dict[str, Role]
    layers: dict[str, dict[str, Layer]]
    personal: frozenset[str]
    replaced: tuple[str, ...]


@dataclass(frozen=True)
class MergedRoles:
    """The roles merged across the layers but not yet validated: each role's ``tables`` of
    fields, by name, as validating a role takes them, and the ``layers`` each field came from,
    by role and field name; the names of the roles that are ``personal``, and of the personal
    roles project roles ``replaced``, in order."""

    tables: dict[str, Any]
    layers: dict[str, dict[str, Layer]]
    personal: frozenset[str]
    replaced: tuple[str, ...]


class UserConfigError(ConfigError):
    """The user config is at fault; the message says how, and the caller names the file."""


class PersonalRoleError(UserConfigError):
    """The personal roles break a rule across the roles that the others keep, so the user
    config is at fault; the message says which rule."""


class _UserConfig(BaseModel):
    """The part of the user config the roles are in; the rest is left to others."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    roles: dict[str, Any] = {}


class _UserRoles(BaseModel):
    model_config = ConfigDict(frozen=True)

    every: RoleFields
    named: dict[RoleName, RoleFields]
    personal: dict[RoleName, Role]


def load_user_roles(data: dict[str, Any], layer: Layer, built_in: Collection[str]) -> UserRoles:
    """The role fields ``data``, the contents of the user config in ``layer``, sets: ``[roles]``'s
    ``model``, ``effort`` and ``runtime`` for every role, and its ``[roles.<name>]`` tables' for
    each; and the personal roles it defines, each a table that names a role that isn't
    ``built_in`` and sets more than those. Its tables but ``[roles]`` are ignored.

    Raises :class:`ConfigError` naming each key that's invalid, as one setting a role's field
    other than its model, effort and runtime is, one that breaks a personal role's rules, or one
    giving a runtime this version of orbit doesn't know.
    """
    try:
        table = _UserConfig.model_validate(data).roles
        roles = _UserRoles.model_validate(_sorted(table, built_in))
    except ValidationError as exc:
        raise ConfigError(_user_role_problems(exc)) from None
    if problems := _unknown_runtimes(roles):
        raise ConfigError("\n".join(problems))
    return UserRoles(layer, roles.every, roles.named, roles.personal)


def _unknown_runtimes(roles: _UserRoles) -> list[str]:
    """Why each runtime the user config's ``roles`` give that this version of orbit doesn't
    know is refused, naming its key, in order: for every role, for each, then each personal
    role's."""
    given = {"roles.runtime": roles.every.runtime}
    given |= {f"roles.{name}.runtime": fields.runtime for name, fields in roles.named.items()}
    given |= {f"roles.{name}.runtime": role.runtime for name, role in roles.personal.items()}
    return [
        unknown_runtime(key, runtime)
        for key, runtime in given.items()
        if runtime is not None and not knows_runtime(runtime)
    ]


def with_runtime(user: UserRoles, runtime: str | None) -> UserRoles:
    """The ``user``'s roles with ``runtime``, if given, as their runtime for every role, in place
    of the user config's: as a run gives the roles a runtime, every role that doesn't name its
    own then runs on it, over the package's runtime too."""
    if runtime is None:
        return user
    return replace(user, every=user.every.model_copy(update={"runtime": runtime}))


def _sorted(table: dict[str, Any], built_in: Collection[str]) -> dict[str, dict[str, Any]]:
    """The user config's ``[roles]`` ``table`` sorted into its values for every role, its
    tables of fields for one role, and its personal roles' tables, as :class:`_UserRoles`
    takes them."""
    every = {key: value for key, value in table.items() if not isinstance(value, dict)}
    tables = {key: value for key, value in table.items() if isinstance(value, dict)}
    return {"every": every, **_role_tables(tables, built_in)}


def _role_tables(
    tables: dict[str, dict[str, Any]], built_in: Collection[str]
) -> dict[str, dict[str, Any]]:
    """The user config's role ``tables``, by role name, sorted into those that set fields of
    a role, ``named``, and those that define a ``personal`` role."""
    personal = {name: fields for name, fields in tables.items() if _defines(name, fields, built_in)}
    named = {name: fields for name, fields in tables.items() if name not in personal}
    return {"named": named, "personal": personal}


def _defines(name: str, fields: dict[str, Any], built_in: Collection[str]) -> bool:
    """Whether the user config's table of ``fields`` for role ``name`` defines a personal role:
    the role isn't ``built_in``, and the table sets more than the fields bound to the machine."""
    return name not in built_in and not fields.keys() <= MACHINE_FIELDS


def _user_role_problems(error: ValidationError) -> str:
    """``error``'s problems with the user config's ``[roles]``, one per line, each naming the
    key and why. A default left unworked out because another field is invalid isn't a problem
    of its own."""
    problems = []
    for problem in error.errors():
        if problem["type"] == "default_factory_not_called":
            continue
        # The first part is the table the roles were validated as: "roles", "every", "named" or
        # "personal".
        table, *rest = problem["loc"]
        where = dotted(("roles", *rest))
        problems.append(f"{where}: {_why(problem, table)}")
    return "\n".join(problems)


def _why(problem: ErrorDetails, table: int | str) -> str:
    """Why ``problem``, found in the ``table`` the user config's roles were validated as, is
    one: a field the user config can't set belongs in the project file, but a personal role's
    are a role's."""
    if problem["type"] == "extra_forbidden" and table != "personal":
        return BELONGS_IN_PROJECT
    return problem["msg"]


class _Roles(BaseModel):
    model_config = ConfigDict(frozen=True)

    roles: dict[RoleName, Role]

    @field_validator("roles")
    @classmethod
    def _consulting_enabled(cls, roles: dict[str, Role]) -> dict[str, Role]:
        return with_enabled_consults(roles)


def resolve_roles(
    package: Mapping[str, Role],
    user: UserRoles,
    project: ProjectRoles,
    rules: RuntimeRules = UNCHECKED,
) -> ResolvedRoles:
    """The ``package``'s roles, then the ``user``'s personal roles, then those ``project`` adds,
    each field resolved most specific first: the ``project``'s value, the ``user``'s value for
    the role (a personal role's own), the ``user``'s value for every role, the ``package``'s
    value, then the default. A role's ``merge`` is the default, which merges if it can, when
    the ``project`` changes its ``verify`` or ``workspace`` but not its ``merge``. A project role
    replaces a personal role of its name entirely; a role the ``user`` only sets fields bound to
    the machine for, and no other layer has, isn't a role. Each role's model is held to the
    models its runtime takes, and each enabled role to what its runtime can do, as ``rules``
    say by runtime.

    Raises :class:`ConfigError` naming the role and field if a role is invalid, or each role
    whose runtime this version of orbit doesn't know, doesn't take its model or can't do what
    it asks (see
    :func:`~orbit.agent.domain.roles.runtime_problems`), and :class:`PersonalRoleError` if the
    personal roles break a rule across the roles that the others keep.
    """
    merged = merge_roles(package, user, project)
    roles = _validated(merged.tables)
    check_runtimes(roles, rules)
    _check_personal(roles, merged.personal)
    return ResolvedRoles(roles, merged.layers, merged.personal, merged.replaced)


def merge_roles(package: Mapping[str, Role], user: UserRoles, project: ProjectRoles) -> MergedRoles:
    """The roles :func:`resolve_roles` resolves from the ``package``, ``user`` and ``project``,
    left unvalidated so that a caller can validate them with the rest of its file."""
    effective, replaced = _personal_in_effect(user, project)
    personal = effective.personal
    names = [*package, *personal, *(name for name in project.roles if name not in package)]
    tables: dict[str, Any] = {}
    layers: dict[str, dict[str, Layer]] = {}
    for name in names:
        tables[name], layers[name] = _resolved(name, package.get(name), effective, project)
    return MergedRoles(tables, layers, frozenset(personal), replaced)


def check_personal(merged: MergedRoles) -> None:
    """Raise :class:`PersonalRoleError`, as :func:`resolve_roles` does, if the ``merged`` roles
    are each valid but the personal ones among them break a rule across the roles that the
    others keep. An invalid role is left for the caller to report with the rest of its file."""
    try:
        roles = _Roles.model_validate({"roles": merged.tables}).roles
    except ValidationError:
        return
    _check_personal(roles, merged.personal)


def replaced_note(name: str) -> str:
    """The startup notice that the project's role ``name`` replaces the user's personal role of
    that name."""
    return f"The project's role {name} replaces your personal role of that name."


def _personal_in_effect(
    user: UserRoles, project: ProjectRoles
) -> tuple[UserRoles, tuple[str, ...]]:
    """The ``user``'s roles with only the personal roles in effect, those whose name the
    ``project`` gives no role, and the names of those it replaces, in order."""
    replaced = tuple(name for name in user.personal if name in project.roles)
    personal = {name: role for name, role in user.personal.items() if name not in replaced}
    return replace(user, personal=personal), replaced


def _validated(data: dict[str, Any]) -> dict[str, Role]:
    """The roles ``data`` describes, by name.

    Raises :class:`ConfigError` naming the role and field if a role is invalid.
    """
    try:
        return _Roles.model_validate({"roles": data}).roles
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None


def _check_personal(roles: Mapping[str, Role], personal: Collection[str]) -> None:
    """Raise :class:`PersonalRoleError`, saying how, if the ``personal`` roles among ``roles``
    break a rule across the roles that the others keep; a rule the others break already is the
    project's to fix."""
    clashes = role_clashes(roles)
    others = {name: role for name, role in roles.items() if name not in personal}
    if clashes and not role_clashes(others):
        raise PersonalRoleError(f"roles: {'; '.join(clashes)}")


def _resolved(
    name: str, built_in: Role | None, user: UserRoles, project: ProjectRoles
) -> tuple[Any, dict[str, Layer]]:
    """The fields of role ``name``, the ``built_in`` one if any, with those the layers set over
    them and its merging worked out as :func:`_merging_worked_out` says, and the layer each came
    from; the project's value as it is if it isn't a table."""
    given = project.roles.get(name, {})
    if not isinstance(given, dict):
        return given, {}
    fields: dict[str, Any] = {} if built_in is None else built_in.model_dump()
    layers = dict.fromkeys(Role.model_fields, DEFAULT)
    for layer, values in _layered(name, built_in, user, project.layer, given):
        fields |= values
        layers |= dict.fromkeys(values, layer)
    return _merging_worked_out(fields, layers, given)


def _merging_worked_out(
    fields: dict[str, Any], layers: dict[str, Layer], given: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Layer]]:
    """A role's resolved ``fields`` and their ``layers``, with its ``merge`` left to the
    default if the project's table of fields ``given`` for it changes what merging depends on
    but not ``merge``: a built-in role then merges if it can, as a role leaving it out does."""
    if "merge" in given or given.keys().isdisjoint(MERGING_NEEDS):
        return fields, layers
    worked_out = {key: value for key, value in fields.items() if key != "merge"}
    return worked_out, layers | {"merge": DEFAULT}


def _layered(
    name: str, built_in: Role | None, user: UserRoles, project: Layer, given: dict[str, Any]
) -> list[tuple[Layer, dict[str, Any]]]:
    """The fields of role ``name`` each layer sets, least specific first, with the layer: the
    ``built_in`` role's, the ``user``'s for every role and for this one, and those ``given`` in
    the ``project`` file."""
    package = {} if built_in is None else _fields_set(built_in)
    return [
        (PACKAGE, package),
        (user.layer, user.every.model_dump(exclude_none=True)),
        (user.layer, _users_own(name, user)),
        (project, given),
    ]


def _users_own(name: str, user: UserRoles) -> dict[str, Any]:
    """The fields the ``user`` config sets for role ``name`` alone: its personal role's, if
    it's one, or else those bound to the machine."""
    personal = user.personal.get(name)
    if personal is not None:
        return _fields_set(personal)
    return user.named.get(name, RoleFields()).model_dump(exclude_none=True)


def _fields_set(role: Role) -> dict[str, Any]:
    """The fields ``role`` was given, rather than left to their defaults."""
    return role.model_dump(include=role.model_fields_set)
