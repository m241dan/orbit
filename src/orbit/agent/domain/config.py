"""Orbit's configuration: the limits, roles, verification steps and setup a project file sets
over the user config's role fields and personal roles and the built-in defaults."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import Setup, VerificationConfig
from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.layers import (
    NO_USER_ROLES,
    Layer,
    MergedRoles,
    ProjectRoles,
    RoleFields,
    UserConfigError,
    UserRoles,
    check_personal,
    merge_roles,
    replaced_note,
)
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import (
    ASKS,
    Role,
    RoleName,
    capability_problems,
    model_problems,
    role_clashes,
    runtime_problems,
    with_enabled_consults,
)
from orbit.agent.domain.runtimes import (
    UNCHECKED,
    ModelAliases,
    RuntimeRules,
)
from orbit.agent.domain.verification import MAX_FALLBACK_FIXES


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_LIMITS = Limits()
_REVIEW_LIMITS = ReviewLimits()


class _FileConfig(_Config):
    """Orbit's limits and roles, as a project file sets them, each field with its description.

    A super review that's due after ``super_review_every`` commits waits while suggested tasks
    are pending.
    """

    fix_attempts: int = Field(
        default=_LIMITS.max_attempts,
        ge=1,
        description="In-session fixes asked of the same agent before its task falls back.",
    )
    max_checks: int = Field(
        default=_LIMITS.max_checks,
        ge=1,
        description="Verification steps an agent may run itself before claiming done.",
    )
    fallback_fixes: int = Field(
        default=MAX_FALLBACK_FIXES,
        ge=1,
        description="Fresh-agent fix tasks one task's chain may queue for each failing step.",
    )
    review_rounds: int = Field(
        default=_REVIEW_LIMITS.rounds,
        ge=1,
        description="Rounds of review-suggested work after which review pauses.",
    )
    super_review_every: int = Field(
        default=_REVIEW_LIMITS.super_every,
        ge=1,
        description="Commits between super reviews, counting yours and the planner's.",
    )
    review_suggestions: int = Field(
        default=_REVIEW_LIMITS.suggestions, ge=1, description="Tasks one review may suggest."
    )
    super_review_suggestions: int = Field(
        default=_REVIEW_LIMITS.super_suggestions,
        ge=1,
        description="Tasks one super review may suggest.",
    )
    roles: dict[RoleName, Role] = Field(
        description="The roles agents work in, by name: the built-in ones, and your own."
    )
    verification: VerificationConfig = Field(
        default=VerificationConfig(), description="How tasks are verified."
    )
    setup: Setup | None = Field(
        default=None, description="How the coder's worktree is set up; with none, nothing is."
    )

    @field_validator("roles")
    @classmethod
    def _lanes_apart(cls, roles: dict[str, Role]) -> dict[str, Role]:
        clashes = role_clashes(roles)
        if clashes:
            raise PydanticCustomError("role_lanes", "; ".join(clashes))
        return with_enabled_consults(roles)

    @property
    def limits(self) -> Limits:
        return Limits(max_attempts=self.fix_attempts, max_checks=self.max_checks)

    @property
    def review_limits(self) -> ReviewLimits:
        return ReviewLimits(
            rounds=self.review_rounds,
            suggestions=self.review_suggestions,
            super_every=self.super_review_every,
            super_suggestions=self.super_review_suggestions,
        )


LATER_TABLES = ("triggers", "mcp", "models")
"""The top-level tables of a later version of orbit's settings."""

LATER_VERSION = (
    "isn't supported by this version of orbit yet; it's in the reference config for a later"
    " version. Leave it commented out."
)
"""Why a setting of a later version of orbit is refused, after the setting's name."""

LIMIT_KEYS = tuple(
    key for key in _FileConfig.model_fields if key not in {"roles", "verification", "setup"}
)
"""The keys of the limits a project file sets, in order."""


class OrbitConfig(_FileConfig):
    """Orbit's configuration in effect: the limits and roles a project file sets, with the names
    of the roles that are ``personal``, defined whole in the user config, the ``notices`` the
    user is told as its workspace starts, and the ``models`` each runtime takes, by name, which
    its roles' models were held to."""

    personal: frozenset[str] = frozenset()
    notices: tuple[str, ...] = ()
    models: dict[str, ModelAliases] = {}


def load_config(
    data: dict[str, Any],
    built_in: Mapping[str, Role],
    user: UserRoles = NO_USER_ROLES,
    path: str | None = None,
    rules: RuntimeRules = UNCHECKED,
) -> OrbitConfig:
    """The configuration ``data`` (the contents of the project file at ``path``, if known)
    describes over the role fields and personal roles ``user`` sets and the defaults, whose
    roles are ``built_in``, with a notice for each personal role a project role replaces, and
    the models each runtime takes, as ``rules`` say.

    A role it names that's built in keeps the fields it doesn't set; any other role is new. Each
    role's fields are resolved as :func:`~orbit.agent.domain.layers.resolve_roles` says; each
    role's model is held to the models its runtime takes, and each enabled role to what its
    runtime can do, as ``rules`` say by runtime. Raises :class:`ConfigError` naming each problem
    if it's invalid, as it is when a role's runtime is one this version of orbit doesn't know,
    doesn't take its model or can't do what the role asks (see
    :func:`~orbit.agent.domain.roles.runtime_problems`), a :class:`PersonalRoleError` if the
    personal roles are at fault, and a :class:`UserConfigError` if a model the user config sets
    isn't one its role's runtime takes, after the project's problems.
    """
    return load_layered(data, built_in, user, path, rules).config


@dataclass(frozen=True)
class LayeredConfig:
    """Orbit's ``config`` in effect, with the layer each of its roles' fields came from,
    ``role_layers``, by role and field name."""

    config: OrbitConfig
    role_layers: Mapping[str, Mapping[str, Layer]]


def load_layered(
    data: dict[str, Any],
    built_in: Mapping[str, Role],
    user: UserRoles = NO_USER_ROLES,
    path: str | None = None,
    rules: RuntimeRules = UNCHECKED,
) -> LayeredConfig:
    """The configuration :func:`load_config` loads from ``data``, ``built_in``, ``user``,
    ``path`` and ``rules``, with the layer each of its roles' fields came from.

    Raises :class:`ConfigError` as :func:`load_config` does, naming the first setting of a later
    version of orbit ``data`` sets, if any, ahead of anything else.
    """
    _refuse_later_settings(data)
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        return LayeredConfig(_validated(data), {})  # Refused: the roles aren't a table.
    merged = merge_roles(built_in, user, ProjectRoles(Layer("project", path), roles))
    check_personal(merged)
    notices = tuple(replaced_note(name) for name in merged.replaced)
    config = _validated({**data, "roles": merged.tables}, merged.personal, notices, rules.models)
    users = _users_models(merged, user)
    asks = _users_asks(merged, user)
    project_set = {
        name: _projects_part(name, role, users, asks) for name, role in config.roles.items()
    }
    _check_projects_roles(config.roles, project_set, asks, rules)
    _check_users_roles(config.roles, users, asks, rules)
    return LayeredConfig(config, merged.layers)


def _projects_part(
    name: str, role: Role, users: Mapping[str, str], asks: Mapping[str, tuple[str, ...]]
) -> Role:
    """Role ``name``, ``role``, as the project answers for it: without the model if the user
    config answers for it, among ``users``, and disabled if the user config answers for any of
    what it asks of its runtime, among ``asks``."""
    # Only the runtime check sees this copy, where being disabled skips what the role asks for;
    # the project's part of what such a role asks is checked on its own.
    update: dict[str, Any] = {"model": None} if name in users else {}
    return role.model_copy(update={**update, "enabled": False} if name in asks else update)


def _check_projects_roles(
    roles: Mapping[str, Role],
    project_set: Mapping[str, Role],
    asks: Mapping[str, tuple[str, ...]],
    rules: RuntimeRules,
) -> None:
    """Raise :class:`ConfigError` naming each of the ``project_set`` roles refused for its
    runtime, as ``rules`` say, then what each role in ``asks`` asks for among ``roles`` in the
    fields the project answers for that its runtime can't do, on a line of its own, if any."""
    problems = runtime_problems(project_set, rules) + [
        problem
        for name, fields in asks.items()
        for problem in _ask_problems(name, roles[name], rules, _others(fields))
    ]
    if problems:
        raise ConfigError("\n".join(problems))


def _others(fields: tuple[str, ...]) -> tuple[str, ...]:
    """The fields asking for what a runtime may not do that aren't among ``fields``."""
    return tuple(field for field in ASKS if field not in fields)


def _users_models(merged: MergedRoles, user: UserRoles) -> dict[str, str]:
    """The ``merged`` roles whose model the ``user`` config answers for, each with the key it's
    named under: those it sets the model of, under ``roles.model`` for the all-roles model or
    ``roles.<name>.model``, and those it sets the runtime of whose model the project doesn't
    set, under ``roles.<name>.model``."""
    return {
        name: _users_model_key(name, user)
        if layers.get("model") == user.layer
        else f"roles.{name}.model"
        for name, layers in merged.layers.items()
        if layers.get("model") == user.layer
        or (layers.get("runtime") == user.layer and not _project_sets(layers, ("model",)))
    }


def _users_asks(merged: MergedRoles, user: UserRoles) -> dict[str, tuple[str, ...]]:
    """The ``merged`` roles whose runtime the ``user`` config sets, each with the fields asking
    for what it may not do that the project doesn't set, for those with any."""
    owned = {
        name: _unset_asks(layers)
        for name, layers in merged.layers.items()
        if layers.get("runtime") == user.layer
    }
    return {name: fields for name, fields in owned.items() if fields}


def _unset_asks(layers: Mapping[str, Layer]) -> tuple[str, ...]:
    """The fields asking for what a runtime may not do that the project doesn't set, as
    ``layers`` say by field."""
    return tuple(field for field in ASKS if not _project_sets(layers, (field,)))


def _project_sets(layers: Mapping[str, Layer], fields: tuple[str, ...]) -> bool:
    """Whether the project sets any of ``fields``, as ``layers`` say by field."""
    return any(field in layers and layers[field].name == "project" for field in fields)


def _users_model_key(name: str, user: UserRoles) -> str:
    """The key the ``user`` config sets role ``name``'s model under."""
    own = user.personal.get(name) or user.named.get(name, RoleFields())
    return f"roles.{name}.model" if own.model is not None else "roles.model"


def _check_users_roles(
    roles: Mapping[str, Role],
    users: Mapping[str, str],
    asks: Mapping[str, tuple[str, ...]],
    rules: RuntimeRules,
) -> None:
    """Raise :class:`UserConfigError` naming each model the user config answers for, under its
    key in ``users``, that the runtime of its role among ``roles`` doesn't take, and what each
    role in ``asks`` asks for in its fields there that its runtime can't do, as ``rules`` say,
    once each, on a line of its own, if any."""
    problems = dict.fromkeys(
        [
            problem
            for name, key in users.items()
            for problem in _model_problem(key, roles[name], rules)
        ]
        + [
            problem
            for name in sorted(asks)
            for problem in _ask_problems(name, roles[name], rules, asks[name])
        ]
    )
    if problems:
        raise UserConfigError("\n".join(problems))


def _model_problem(key: str, role: Role, rules: RuntimeRules) -> list[str]:
    """Why ``role``'s model, set under ``key``, is refused, as
    :func:`~orbit.agent.domain.roles.model_problems` says with the models ``rules`` say its
    runtime takes."""
    return model_problems(key, role, rules.models.get(role.runtime))


def _ask_problems(name: str, role: Role, rules: RuntimeRules, fields: tuple[str, ...]) -> list[str]:
    """What role ``name``, ``role``, asks for in its ``fields`` that its runtime can't do, as
    :func:`~orbit.agent.domain.roles.capability_problems` says, if it's enabled and ``rules``
    say what its runtime can do; nothing otherwise."""
    can = rules.capabilities.get(role.runtime)
    if not role.enabled or can is None:
        return []
    return capability_problems(name, role, can, fields)


def _refuse_later_settings(data: dict[str, Any]) -> None:
    """Raise :class:`ConfigError` naming the first setting of a later version of orbit that
    ``data`` sets, if it sets one."""
    setting = next(_later_settings(data), None)
    if setting is not None:
        raise ConfigError(f"{setting} {LATER_VERSION}")


def _later_settings(data: dict[str, Any]) -> Iterator[str]:
    """The settings of a later version of orbit ``data`` sets, in order, each named as the
    reference config names it: its :data:`LATER_TABLES`, and its roles' ``tools`` tables."""
    for key, value in data.items():
        if key in LATER_TABLES:
            yield _table_name(key, value)
        elif key == "roles":
            yield from _later_roles_settings(value)


def _later_roles_settings(roles: object) -> Iterator[str]:
    """The settings of a later version of orbit the ``roles`` table sets, role by role."""
    if not isinstance(roles, dict):
        return
    for name, fields in roles.items():
        yield from _later_role_settings(name, fields)


def _table_name(key: str, value: object) -> str:
    """The later version's top-level ``key``, set to ``value``, named by its first table,
    ``[key.<name>]``, or as ``[key]`` if it has none."""
    if not isinstance(value, dict):
        return f"[{key}]"
    names = [name for name, table in value.items() if isinstance(table, dict)]
    return f"[{key}.{names[0]}]" if names else f"[{key}]"


def _later_role_settings(name: str, fields: object) -> Iterator[str]:
    """The settings of a later version of orbit the ``fields`` of role ``name`` set: its
    ``tools`` as a table rather than a list of orbit's tools."""
    if isinstance(fields, dict) and isinstance(fields.get("tools"), dict):
        yield f"[roles.{name}.tools]"


def _validated(
    data: dict[str, Any],
    personal: frozenset[str] = frozenset(),
    notices: tuple[str, ...] = (),
    models: Mapping[str, ModelAliases] = UNCHECKED.models,
) -> OrbitConfig:
    """The configuration ``data`` describes, its roles resolved, with the names of those that
    are ``personal``, the ``notices`` to tell the user and the ``models`` each runtime takes.
    Raises :class:`ConfigError` if it's invalid."""
    try:
        settings = _FileConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    return OrbitConfig(**dict(settings), personal=personal, notices=notices, models=dict(models))
