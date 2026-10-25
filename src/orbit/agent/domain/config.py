"""Orbit's configuration: the limits, roles, verification steps and setup a project file sets
over the user config's role fields and personal roles and the built-in defaults."""

from collections.abc import Mapping
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
    replaced_note,
    resolve_roles,
)
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import Role, role_clashes
from orbit.agent.domain.verification import MAX_FALLBACK_FIXES


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_LIMITS = Limits()
_REVIEW_LIMITS = ReviewLimits()


class _FileConfig(_Config):
    """Orbit's limits and roles, as a project file sets them.

    ``fix_attempts``: in-session fixes, asked of the same agent before its task falls back.
    ``max_checks``: verification steps an agent may run itself before claiming done.
    ``fallback_fixes``: fresh-agent fix tasks one task's chain may queue for each failing step.
    ``review_rounds``: rounds of review-suggested work after which review pauses.
    ``super_review_every``: commits between super reviews, counting your own and the planner's;
    a super review that's due waits while suggested tasks are pending.
    ``review_suggestions`` / ``super_review_suggestions``: tasks one review may suggest.
    ``verification``: how tasks are verified.
    ``setup``: how the coder's worktree is set up; ``None`` sets nothing up.
    """

    fix_attempts: int = Field(default=_LIMITS.max_attempts, ge=1)
    max_checks: int = Field(default=_LIMITS.max_checks, ge=1)
    fallback_fixes: int = Field(default=MAX_FALLBACK_FIXES, ge=1)
    review_rounds: int = Field(default=_REVIEW_LIMITS.rounds, ge=1)
    super_review_every: int = Field(default=_REVIEW_LIMITS.super_every, ge=1)
    review_suggestions: int = Field(default=_REVIEW_LIMITS.suggestions, ge=1)
    super_review_suggestions: int = Field(default=_REVIEW_LIMITS.super_suggestions, ge=1)
    roles: dict[str, Role]
    verification: VerificationConfig = VerificationConfig()
    setup: Setup | None = None

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


class OrbitConfig(_FileConfig):
    """Orbit's configuration in effect: the limits and roles a project file sets, with the names
    of the roles that are ``personal``, defined whole in the user config, and the ``notices``
    the user is told as its workspace starts."""

    personal: frozenset[str] = frozenset()
    notices: tuple[str, ...] = ()


def load_config(
    data: dict[str, Any],
    built_in: Mapping[str, Role],
    user: UserRoles = NO_USER_ROLES,
    path: str | None = None,
) -> OrbitConfig:
    """The configuration ``data`` (the contents of the project file at ``path``, if known)
    describes over the role fields and personal roles ``user`` sets and the defaults, whose
    roles are ``built_in``, with a notice for each personal role a project role replaces.

    A role it names that's built in keeps the fields it doesn't set; any other role is new. Each
    role's fields are resolved as :func:`resolve_roles` says. Raises :class:`ConfigError` if
    it's invalid, a :class:`PersonalRoleError` if the personal roles are at fault.
    """
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        return _validated(data)  # Refused: the roles aren't a table.
    resolved = resolve_roles(built_in, user, ProjectRoles(Layer("project", path), roles))
    notices = tuple(replaced_note(name) for name in resolved.replaced)
    return _validated({**data, "roles": resolved.roles}, resolved.personal, notices)


def _validated(
    data: dict[str, Any], personal: frozenset[str] = frozenset(), notices: tuple[str, ...] = ()
) -> OrbitConfig:
    """The configuration ``data`` describes, its roles resolved, with the names of those that
    are ``personal`` and the ``notices`` to tell the user. Raises :class:`ConfigError` if it's
    invalid."""
    try:
        settings = _FileConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    return OrbitConfig(**dict(settings), personal=personal, notices=notices)
