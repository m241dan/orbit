"""The layers orbit's configuration is resolved from, and the roles resolved across them.

From least to most specific: orbit's built-in defaults, in no file; the package's roles; the
user config, which sets only the role fields bound to the machine, its model and effort, for
every role or for one; and the project file. A role's field is the most specific layer's value.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.roles import Role
from orbit.protocol import Effort

LayerName = Literal["default", "package", "user", "project"]

BELONGS_IN_PROJECT = (
    "belongs in the project's .orbit/config.toml; the user config sets only a role's model and"
    " effort"
)
"""Why the user config can't set a role's field other than its model and effort."""


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
    """The fields of a role the user config sets, those bound to the machine: its ``model`` and
    ``effort``, each ``None`` if it doesn't set it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None
    effort: Effort | None = None


@dataclass(frozen=True)
class UserRoles:
    """The role fields the user config in ``layer`` sets: those for ``every`` role, and those
    for each role it ``named``."""

    layer: Layer
    every: RoleFields = field(default_factory=RoleFields)
    named: Mapping[str, RoleFields] = field(default_factory=dict)


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
    from, by role and field name."""

    roles: dict[str, Role]
    layers: dict[str, dict[str, Layer]]


class _UserConfig(BaseModel):
    """The part of the user config the roles are in; the rest is left to others."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    roles: dict[str, Any] = {}


class _UserRoles(BaseModel):
    model_config = ConfigDict(frozen=True)

    every: RoleFields
    named: dict[str, RoleFields]


def load_user_roles(data: dict[str, Any], layer: Layer) -> UserRoles:
    """The role fields ``data``, the contents of the user config in ``layer``, sets: ``[roles]``'s
    ``model`` and ``effort`` for every role, and its ``[roles.<name>]`` tables' for each; its
    tables but ``[roles]`` are ignored.

    Raises :class:`ConfigError` naming each key that's invalid, as one setting a role's field
    other than its model and effort is.
    """
    try:
        table = _UserConfig.model_validate(data).roles
        every = {key: value for key, value in table.items() if not isinstance(value, dict)}
        named = {key: value for key, value in table.items() if isinstance(value, dict)}
        roles = _UserRoles.model_validate({"every": every, "named": named})
    except ValidationError as exc:
        raise ConfigError(_user_role_problems(exc)) from None
    return UserRoles(layer, roles.every, roles.named)


def _user_role_problems(error: ValidationError) -> str:
    """``error``'s problems with the user config's ``[roles]``, one per line, each naming the
    key and why; a field the user config can't set belongs in the project file."""
    problems = []
    for problem in error.errors():
        # The first part is the table the roles were validated as: "roles", "every" or "named".
        where = ".".join(["roles", *(str(part) for part in problem["loc"][1:])])
        why = BELONGS_IN_PROJECT if problem["type"] == "extra_forbidden" else problem["msg"]
        problems.append(f"{where}: {why}")
    return "\n".join(problems)


class _Roles(BaseModel):
    model_config = ConfigDict(frozen=True)

    roles: dict[str, Role]


def resolve_roles(
    package: Mapping[str, Role], user: UserRoles, project: ProjectRoles
) -> ResolvedRoles:
    """The ``package``'s roles and those ``project`` adds, after them, each field resolved
    most specific first: the ``project``'s value, the ``user``'s value for the role, the
    ``user``'s value for every role, the ``package``'s value, then the default. A role only the
    ``user`` names isn't a role.

    Raises :class:`ConfigError` naming the role and field if a role is invalid.
    """
    names = [*package, *(name for name in project.roles if name not in package)]
    data: dict[str, Any] = {}
    layers: dict[str, dict[str, Layer]] = {}
    for name in names:
        data[name], layers[name] = _resolved(name, package.get(name), user, project)
    try:
        roles = _Roles.model_validate({"roles": data}).roles
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    return ResolvedRoles(roles, layers)


def _resolved(
    name: str, built_in: Role | None, user: UserRoles, project: ProjectRoles
) -> tuple[Any, dict[str, Layer]]:
    """The fields of role ``name``, the ``built_in`` one if any, with those the layers set over
    them, and the layer each came from; the project's value as it is if it isn't a table."""
    given = project.roles.get(name, {})
    if not isinstance(given, dict):
        return given, {}
    fields: dict[str, Any] = {} if built_in is None else built_in.model_dump()
    layers = dict.fromkeys(Role.model_fields, DEFAULT)
    for layer, values in _layered(name, built_in, user, project.layer, given):
        fields |= values
        layers |= dict.fromkeys(values, layer)
    return fields, layers


def _layered(
    name: str, built_in: Role | None, user: UserRoles, project: Layer, given: dict[str, Any]
) -> list[tuple[Layer, dict[str, Any]]]:
    """The fields of role ``name`` each layer sets, least specific first, with the layer: the
    ``built_in`` role's, the ``user``'s for every role and for this one, and those ``given`` in
    the ``project`` file."""
    package = {} if built_in is None else built_in.model_dump(include=built_in.model_fields_set)
    named = user.named.get(name, RoleFields())
    return [
        (PACKAGE, package),
        (user.layer, user.every.model_dump(exclude_none=True)),
        (user.layer, named.model_dump(exclude_none=True)),
        (project, given),
    ]
