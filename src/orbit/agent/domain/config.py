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
    ProjectRoles,
    UserRoles,
    check_personal,
    merge_roles,
    replaced_note,
)
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import Role, RoleName, check_runtimes, role_clashes
from orbit.agent.domain.runtimes import UNCHECKED, ModelAliases, RuntimeRules
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
        return roles

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
    personal roles are at fault.
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
    check_runtimes(config.roles, rules)
    return LayeredConfig(config, merged.layers)


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
