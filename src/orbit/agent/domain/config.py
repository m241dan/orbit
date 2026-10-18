"""Orbit's configuration: the limits, roles, verification steps and setup a project file sets
over the built-in defaults."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import Setup, VerificationConfig
from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import Role
from orbit.agent.domain.verification import MAX_FALLBACK_FIXES


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_LIMITS = Limits()
_REVIEW_LIMITS = ReviewLimits()


class OrbitConfig(_Config):
    """Orbit's limits and roles.

    ``fix_attempts``: in-session fixes, asked of the same agent before its task falls back.
    ``max_checks``: verification steps an agent may run itself before claiming done.
    ``fallback_fixes``: fresh-agent fix tasks one task's chain may queue for each failing step.
    ``review_rounds``: rounds of review-suggested work after which review pauses.
    ``super_review_every``: commits between super reviews.
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


def load_config(data: dict[str, Any], built_in: Mapping[str, Role]) -> OrbitConfig:
    """The configuration ``data`` (a project file's contents) describes over the defaults, whose
    roles are ``built_in``.

    A role it names that's built in keeps the fields it doesn't set; any other role is new.
    Raises :class:`ConfigError` if it's invalid.
    """
    data = dict(data)
    roles = data.get("roles", {})
    if isinstance(roles, dict):
        data["roles"] = _with_built_in_roles(roles, built_in)
    try:
        return OrbitConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None


def _with_built_in_roles(roles: dict[str, Any], built_in: Mapping[str, Role]) -> dict[str, Any]:
    merged: dict[str, Any] = {name: role.model_dump() for name, role in built_in.items()}
    for name, fields in roles.items():
        base = merged.get(name, {})
        merged[name] = {**base, **fields} if isinstance(fields, dict) else fields
    return merged
