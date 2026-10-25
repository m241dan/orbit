"""The runtimes agents run on, which a role names: those this version of orbit knows, what orbit
asks of one, what it can do, the models it takes, and the failures it tells in its agents'
errors.

A runtime's models are aliases, from the tables the package and the user config give, each
resolving to an id the runtime takes, and full ids, which the runtime says how to tell. The
tables aren't kept here: whoever reads them passes in their contents.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from orbit.agent.domain.config_errors import ConfigError, validation_problems
from orbit.protocol import Effort, ToolAccess

CLAUDE = "claude"
"""Claude Code, the runtime a role runs on unless it says otherwise."""

KIRO = "kiro"
"""Kiro, through its CLI, ``kiro-cli``."""

RUNTIME_NAMES = (CLAUDE, KIRO)
"""The runtimes this version of orbit knows, by name, in order."""


def knows_runtime(runtime: str) -> bool:
    """Whether this version of orbit knows the runtime ``runtime``."""
    return runtime in RUNTIME_NAMES


def known_runtimes() -> str:
    """What says which runtimes this version of orbit knows."""
    return f"this version of orbit knows the runtimes {', '.join(RUNTIME_NAMES)}."


def unknown_runtime(key: str, runtime: str) -> str:
    """Why the setting ``key``, giving the ``runtime`` this version of orbit doesn't know, is
    refused."""
    return f'{key} = "{runtime}": {known_runtimes()}'


def not_on_path(runtime: str, roles: Sequence[str], executable: str, install: str) -> str:
    """Why orbit can't start ``roles``' agents on ``runtime``: its ``executable`` isn't on
    PATH, so the user should do as ``install`` says or change their runtime."""
    return (
        f"Roles {', '.join(roles)} use the runtime {runtime}, but {executable} isn't on PATH."
        f" {install}, or set those roles' runtime to one you have."
    )


def asks_beyond(role: str, runtime: str, request: str, supported: str) -> str:
    """Why role ``role`` is refused: it asks for ``request``, but its ``runtime`` does only as
    ``supported`` says."""
    return f"roles.{role} asks for {request}, but its runtime {runtime} {supported}."


def accepted(setting: str, levels: Sequence[str]) -> str:
    """What says a runtime accepts only ``levels`` of ``setting``, in order, or none."""
    if not levels:
        return f"accepts no {setting} setting"
    *others, last = [f'"{level}"' for level in levels]
    listed = f"{', '.join(others)} or {last}" if others else last
    return f"accepts only {setting} {listed}"


FailureKind = Literal["usage_limit", "api_unavailable"]
"""What stopped an agent: its runtime's usage limit, or its runtime's API not answering."""


@dataclass(frozen=True)
class Capabilities:
    """What a runtime can do: carry a conversation on (``resume``), report what its agents'
    turns cost and its usage limits (``usage``) and cap an agent's turns (``max_turns``), and the
    levels of ``tool_access`` and ``effort`` it can run an agent at."""

    resume: bool
    usage: bool
    max_turns: bool
    tool_access: frozenset[ToolAccess]
    effort: frozenset[Effort]


@dataclass(frozen=True)
class Failure:
    """An agent's error as its runtime tells it: the ``kind`` of failure, when the limit it hit
    resets, ``resets_at``, if the error says, and how long a run it stopped waits before trying
    again when nothing says when it resets, ``retry``, if its runtime says, as one whose limits
    reset less often than Claude's does."""

    kind: FailureKind
    resets_at: datetime | None = None
    retry: timedelta | None = None


@dataclass(frozen=True)
class FullIdRule:
    """How a runtime tells a full model id: a name the regular expression ``pattern`` matches
    whole, as ``described`` says in words."""

    pattern: str
    described: str

    def matches(self, model: str) -> bool:
        """Whether ``model`` is a full id by this rule."""
        return re.fullmatch(self.pattern, model, re.DOTALL) is not None


def starting_with(prefix: str) -> FullIdRule:
    """The rule of a runtime whose full ids are the names starting with ``prefix``."""
    return FullIdRule(f"{re.escape(prefix)}.*", f'starting with "{prefix}"')


@dataclass(frozen=True)
class ModelAliases:
    """The models the runtime ``runtime`` takes: its ``aliases``, each with the id it resolves
    to, in order, and its full ids, the names its ``full_id`` rule takes."""

    runtime: str
    aliases: Mapping[str, str]
    full_id: FullIdRule


class UnknownModel(ValueError):
    """A ``model`` its runtime's ``models`` don't resolve: neither an alias nor a full id."""

    def __init__(self, model: str, models: ModelAliases) -> None:
        super().__init__(f'The model "{model}" doesn\'t resolve: {known_models(models)}.')
        self.model = model
        self.models = models


def known_models(models: ModelAliases) -> str:
    """What says which models ``models``' runtime takes: its aliases, in order, and its full
    ids."""
    full = f"a full id {models.full_id.described}"
    if not models.aliases:
        return f"the runtime {models.runtime} takes {full}"
    listed = ", ".join(models.aliases)
    return f"the runtime {models.runtime} takes the aliases {listed} or {full}"


def unknown_model(key: str, model: str, models: ModelAliases) -> str:
    """Why the setting ``key``, giving a ``model`` its runtime's ``models`` don't resolve, is
    refused."""
    return f'{key} = "{model}": {known_models(models)}.'


def resolves(models: ModelAliases, model: str) -> bool:
    """Whether a runtime's ``models`` resolve ``model``: it's one of their aliases, or a full
    id."""
    return model in models.aliases or models.full_id.matches(model)


def resolve_model(models: ModelAliases, model: str) -> str:
    """The id ``model`` resolves to among a runtime's ``models``: an alias's target, or a full
    id as it is.

    Raises :class:`UnknownModel`, listing the aliases, if it's neither.
    """
    if not resolves(models, model):
        raise UnknownModel(model, models)
    return models.aliases.get(model, model)


@dataclass(frozen=True)
class RuntimeRules:
    """What roles are held to by the runtimes they run on, by runtime name: what each can do,
    ``capabilities``, and the models each takes, ``models``. A runtime missing from either
    isn't checked for it."""

    capabilities: Mapping[str, Capabilities] = field(default_factory=dict)
    models: Mapping[str, ModelAliases] = field(default_factory=dict)


UNCHECKED = RuntimeRules()
"""No runtime's rules: roles checked against them aren't held to what their runtime can do or
takes."""

ModelName = Annotated[str, StringConstraints(min_length=1)]
"""An alias, or the id it resolves to."""


class _RuntimeTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[ModelName, ModelName] = {}


class _RuntimeTables(BaseModel):
    """The part of a file the runtimes are in; the rest is left to others."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtimes: dict[str, _RuntimeTable] = {}


def load_model_tables(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The aliases the ``[runtimes.<name>.models]`` tables of ``data``, a file's contents, give
    each runtime, by name, each with the id it resolves to, in order. Its tables but
    ``[runtimes]`` are ignored.

    Raises :class:`ConfigError` naming each key that's invalid, or each runtime this version of
    orbit doesn't know.
    """
    try:
        tables = _RuntimeTables.model_validate(data).runtimes
    except ValidationError as exc:
        raise ConfigError(validation_problems(exc)) from None
    unknown = [f"runtimes.{name}: {known_runtimes()}" for name in tables if not knows_runtime(name)]
    if unknown:
        raise ConfigError("\n".join(unknown))
    return {name: dict(table.models) for name, table in tables.items()}


def model_aliases(
    package: Mapping[str, Mapping[str, str]],
    user: Mapping[str, Mapping[str, str]],
    full_ids: Mapping[str, FullIdRule],
) -> dict[str, ModelAliases]:
    """The models each runtime in ``full_ids`` takes, by name, in order: the ``package``'s
    aliases for it, with the ``user``'s added or in their place, and its full ids, those its rule
    there takes."""
    return {
        name: ModelAliases(name, {**package.get(name, {}), **user.get(name, {})}, rule)
        for name, rule in full_ids.items()
    }
