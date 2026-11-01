"""Roles held to the runtimes they run on, when the config loads and when a task or a
conversation asks for its settings.

The runtimes the enabled roles run on are those orbit must start agents on. A role's runtime must
be one this version of orbit knows; its model, and a task's or a conversation's, must be one its
runtime takes; and an enabled role, and a task's or a conversation's settings, may ask only for
what its runtime can do.
"""

from collections.abc import Mapping, Sequence
from typing import get_args

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import Role
from orbit.agent.domain.runtimes import (
    UNCHECKED,
    Capabilities,
    ModelAliases,
    RuntimeRules,
    accepted,
    asks_beyond,
    known_models,
    knows_runtime,
    resolves,
    unknown_model,
    unknown_runtime,
)
from orbit.protocol import INVALID, Effort, Settings, ToolAccess

TOOL_ACCESSES: tuple[ToolAccess, ...] = get_args(ToolAccess)
"""Every level of tool access, in order."""

EFFORTS: tuple[Effort, ...] = get_args(Effort)
"""Every level of effort, in order."""


def runtime_roles(roles: Mapping[str, Role]) -> dict[str, tuple[str, ...]]:
    """The runtimes the enabled roles among ``roles`` run on, in order, each with the names of
    those roles, in order: those orbit must be able to start agents on."""
    runtimes: dict[str, list[str]] = {}
    for name, role in roles.items():
        if role.enabled:
            runtimes.setdefault(role.runtime, []).append(name)
    return {runtime: tuple(names) for runtime, names in runtimes.items()}


def runtime_problems(roles: Mapping[str, Role], rules: RuntimeRules = UNCHECKED) -> list[str]:
    """Why each of ``roles`` is refused for its runtime, naming it and its runtime, in order: its
    runtime is one this version of orbit doesn't know, its model is one its runtime doesn't take,
    as ``rules`` say by runtime (see :func:`model_problems`), or it's enabled and asks for what
    its runtime can't do, as they say (see :func:`capability_problems`); a runtime they don't
    name isn't checked."""
    return [
        problem for name, role in roles.items() for problem in _role_problems(name, role, rules)
    ]


def _role_problems(name: str, role: Role, rules: RuntimeRules) -> list[str]:
    """Why role ``name``, ``role``, is refused for its runtime, as :func:`runtime_problems`
    says."""
    if not knows_runtime(role.runtime):
        return [unknown_runtime(f"roles.{name}.runtime", role.runtime)]
    problems = model_problems(f"roles.{name}.model", role, rules.models.get(role.runtime))
    can = rules.capabilities.get(role.runtime)
    if role.enabled and can is not None:
        problems += capability_problems(name, role, can)
    return problems


def model_problems(key: str, role: Role, models: ModelAliases | None) -> list[str]:
    """Why ``role`` is refused for its model, set under ``key``, naming the key, the model and
    the aliases, if its runtime's ``models`` don't resolve it; nothing for no model, or no
    ``models`` to resolve it with."""
    if role.model is None or models is None or resolves(models, role.model):
        return []
    return [unknown_model(key, role.model, models)]


ASKS = ("tool_access", "effort", "conversation")
"""The role fields that ask for what a runtime may not be able to do."""


def capability_problems(
    name: str, role: Role, capabilities: Capabilities, fields: Sequence[str] = ASKS
) -> list[str]:
    """What role ``name``, ``role``, asks for in its ``fields`` that its runtime can't do, as
    its ``capabilities`` say, in order, each naming the role, the runtime, the request and what
    the runtime does: a level of tool access or effort it doesn't accept, and a conversation it
    can't carry on."""
    found = {
        "tool_access": _level_ask(
            "tool_access", role.tool_access, capabilities.tool_access, TOOL_ACCESSES
        ),
        "effort": _level_ask("effort", role.effort, capabilities.effort, EFFORTS),
        "conversation": _conversation_ask(role.conversation, capabilities.resume),
    }
    asks = [ask for field in ASKS if field in fields for ask in found[field]]
    return [asks_beyond(name, role.runtime, request, supported) for request, supported in asks]


def _conversation_ask(conversation: bool, resume: bool) -> list[tuple[str, str]]:
    """A ``conversation``, if it's asked for of a runtime without ``resume``, with what says it
    doesn't support it; nothing otherwise."""
    return (
        [("conversation = true", "doesn't support resume")] if conversation and not resume else []
    )


def _level_ask(
    setting: str, level: str | None, supported: frozenset[str], levels: Sequence[str]
) -> list[tuple[str, str]]:
    """``level`` of ``setting``, if it's asked for and isn't ``supported``, with what says which
    of the setting's ``levels`` are, in order; nothing otherwise."""
    if level is None or level in supported:
        return []
    shown = [each for each in levels if each in supported]
    return [(f'{setting} "{level}"', accepted(setting, shown))]


def check_runtimes(roles: Mapping[str, Role], rules: RuntimeRules = UNCHECKED) -> None:
    """Raise :class:`ConfigError` naming each of ``roles`` refused for its runtime, as
    :func:`runtime_problems` says with ``rules``, on a line of its own, if any."""
    problems = runtime_problems(roles, rules)
    if problems:
        raise ConfigError("\n".join(problems))


def check_model(name: str, model: str | None, models: ModelAliases | None) -> None:
    """Refuse ``model``, a task's or a conversation's in role ``name``, unless its runtime's
    ``models`` resolve it; no model, or no ``models`` to resolve it with, isn't refused.

    Raises :class:`Refused`, coded ``invalid``, naming the model and the aliases.
    """
    if model is None or models is None or resolves(models, model):
        return
    raise Refused(INVALID, f"{name}'s model \"{model}\" doesn't resolve: {known_models(models)}.")


def check_settings(name: str, runtime: str, settings: Settings, capabilities: Capabilities) -> None:
    """Refuse ``settings``, a task's or a conversation's in role ``name`` on ``runtime``, if
    they ask for a level of tool access or effort its ``capabilities`` don't accept; a level
    left unset isn't refused.

    Raises :class:`Refused`, coded ``invalid``, naming the role, the runtime, the request and
    what the runtime accepts.
    """
    asks = _level_ask("tool_access", settings.tool_access, capabilities.tool_access, TOOL_ACCESSES)
    asks += _level_ask("effort", settings.effort, capabilities.effort, EFFORTS)
    if asks:
        request, supported = asks[0]
        message = f"Role {name}'s runtime {runtime} {supported}, so it can't take {request}."
        raise Refused(INVALID, message)
