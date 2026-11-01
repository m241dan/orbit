"""Orbit's reference project configuration: every setting a project's ``.orbit/config.toml`` can
change, commented out at its default under a line saying what it does, generated from orbit's
models and the built-in roles and triggers shipped with it.

Run as ``python -m orbit.agent.external.reference_config [path]`` to write it, by default to
``REFERENCE_PATH``, the copy shipped with orbit.
"""

import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, TypeAdapter

from orbit.agent.domain.checks import Setup, VerificationConfig, VerificationSet
from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig, load_config
from orbit.agent.domain.config_update import FOR_EXAMPLE
from orbit.agent.domain.roles import Role
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.agent.external.built_in_triggers import read_built_in_triggers, read_template_files

REFERENCE_PATH = Path("src/orbit/agent/external/reference/config.toml")
"""Where the reference is written, relative to the current directory: the copy shipped with
orbit."""

REFERENCE_WIDTH = 100
"""The most columns a line of the reference takes."""

HEADER = (
    "orbit's project configuration, every setting commented out at its default.",
    "Uncomment the settings you change; the others keep their defaults.",
)

EXAMPLE_SET = "mutation"
EXAMPLE_STEP = VerificationStep("Mutation tests", ("uv", "run", "mutmut", "run"), set=EXAMPLE_SET)
EXAMPLE_SET_FIELDS = VerificationSet(holds_merges=True)
"""The example of a verification step, in a named set, and of that set's table."""
EXAMPLE_SETUP = Setup(command=("uv", "sync"), watch=("uv.lock",))
EXAMPLE_ROLE = "license-reviewer"
EXAMPLE_ROLE_FIELDS = Role(
    instructions="roles/license-reviewer.md",
    verify=False,
    lane="licenses",
    workspace="snapshot",
    merge=False,
)
"""The example of a role of a project's own: just the fields it sets."""
EXAMPLE_SKILLS = SkillMapping(paths=("src",), use=("application", "python"))
"""The example of skills for the code under some paths."""

LATER_HEADING = "Coming in a later version of orbit; leave commented out until then."

LATER = (
    (
        "An MCP server roles may use by name, which orbit proxies through its own.",
        "[mcp.github]",
        "The command that starts it, as a process of orbit's.",
        'command = ["github-mcp-server", "stdio"]',
        "Its environment, naming variables whose values come from the control plane, never here.",
        'env = { GITHUB_TOKEN = "$GITHUB_TOKEN" }',
        "Whether the agents share one connection to it, rather than each having its own.",
        "shared = true",
    ),
    (
        "An MCP server that's already running.",
        "[mcp.semgrep]",
        "The URL it serves MCP at.",
        'url = "http://127.0.0.1:3010/mcp"',
    ),
    (
        "The tools a role's agents may use, in place of its tool_access and tools.",
        "[roles.license-reviewer.tools]",
        "The orbit tools its agents may call.",
        'orbit = ["get_task", "complete_task", "suggest_task", "report_progress"]',
        'What its agents may do in their workspace: "none", "read", "write" or "shell".',
        'workspace = "read"',
        "The MCP servers its agents may use, each declared in an [mcp.<name>] table.",
        'mcp = ["semgrep"]',
    ),
    (
        "A model endpoint for the built-in harness, which a role's model names.",
        "[models.local-coder]",
        "The provider and model id, as LangChain reads them.",
        'model = "openai:qwen3-coder"',
        "The endpoint's URL, such as an OpenAI-compatible server's.",
        'base_url = "http://127.0.0.1:11434/v1"',
        "How many tokens the model's context holds.",
        "context_window = 32768",
    ),
)
"""The settings of a later version of orbit, in blocks: each key or table under the line
explaining it."""


def reference_config() -> str:
    """The reference config: a short header; the limits at their defaults; examples of a
    verification step in a named set, of that set's table, and of a setup; each built-in role's
    table with every field at its shipped
    value, a list too long for a line in short; an example of a role of a project's own; each
    shipped trigger's table with every field at its shipped value; an example of skills by path;
    and the settings of a later version of orbit under :data:`LATER_HEADING`. Everything is
    commented out, each key under its description.
    """
    roles = read_built_in_roles()
    files = read_instruction_files()
    templates = read_template_files()
    config = _descriptions(OrbitConfig)
    verification = _descriptions(VerificationConfig)
    sections = [
        _comments(HEADER),
        _limits(load_config({}, roles), config),
        _example(verification["steps"], "[[verification.steps]]", EXAMPLE_STEP),
        _example(verification["sets"], f"[verification.sets.{EXAMPLE_SET}]", EXAMPLE_SET_FIELDS),
        _example(config["setup"], "[setup]", EXAMPLE_SETUP),
        _comments([config["roles"]]),
        *(_built_in(name, role, files[name]) for name, role in roles.items()),
        _example(
            "A role of your own, which must set its instructions.",
            f"[roles.{EXAMPLE_ROLE}]",
            EXAMPLE_ROLE_FIELDS,
        ),
        _comments([config["triggers"]]),
        *(
            _shipped_trigger(name, trigger, templates.get(name))
            for name, trigger in read_built_in_triggers().items()
        ),
        _example(config["skills"], "[[skills]]", EXAMPLE_SKILLS),
        _comments([LATER_HEADING]),
        *map(_comments, LATER),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def write_reference(path: Path) -> None:
    """Writes the reference config to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reference_config(), encoding="utf-8")


def _descriptions(model: type[Any]) -> dict[str, str]:
    """The description of each of ``model``'s fields that has one, by name, in order."""
    properties: dict[str, dict[str, Any]] = TypeAdapter(model).json_schema()["properties"]
    return {
        key: field["description"] for key, field in properties.items() if "description" in field
    }


def _comments(lines: Iterable[str]) -> list[str]:
    """``lines``, each commented out."""
    return [f"# {line}" for line in lines]


def _key(key: str, value: Any) -> str:
    """The commented-out line giving ``key`` its ``value``, as TOML, a table inline."""
    return f"# {key} = {_inline(value).as_string()}"


def _inline(value: Any) -> Any:
    """``value`` as a TOML item, a table as an inline one, as a key's value is on its line."""
    if not isinstance(value, dict):
        return tomlkit.item(value)
    table = tomlkit.inline_table()
    table.update(value)
    return table


def _described(values: Mapping[str, Any], descriptions: Mapping[str, str]) -> list[str]:
    """Each of ``values``, by key, commented out under its line of ``descriptions``."""
    return [
        line
        for key, value in values.items()
        for line in (f"# {descriptions[key]}", _key(key, value))
    ]


def _limits(defaults: OrbitConfig, descriptions: Mapping[str, str]) -> list[str]:
    """The limits at their ``defaults``, each under its line of ``descriptions``."""
    return _described({key: getattr(defaults, key) for key in LIMIT_KEYS}, descriptions)


def _example(description: str, header: str, example: BaseModel | VerificationStep) -> list[str]:
    """The table under ``header`` that sets the fields ``example`` does, under ``description``."""
    values = TypeAdapter(type(example)).dump_python(example, mode="json", exclude_unset=True)
    return [
        f"# {description} {FOR_EXAMPLE}",
        f"# {header}",
        *_described(values, _descriptions(type(example))),
    ]


def _built_in(name: str, role: Role, file: str) -> list[str]:
    """Built-in role ``name``'s table: each of ``role``'s fields under its description, its
    instructions being orbit's own ``file``."""
    descriptions = _descriptions(Role)
    lines = [f"# [roles.{name}]"]
    for key, value in role.model_dump(mode="json").items():
        lines += [f"# {descriptions[key]}", _role_field(name, key, value, file)]
    return lines


def _role_field(name: str, key: str, value: Any, file: str) -> str:
    """The commented-out line of built-in role ``name``'s field ``key``, at its ``value``: its
    instructions as orbit's own ``file``, a field it leaves unset as unset, as TOML has no value
    for that, and a list too long for a line in short (see :func:`_fitted`)."""
    if key == "instructions":
        return f"# instructions: orbit's own {file}"
    if value is None:
        return f"# {key}: unset"
    return _fitted(name, key, value)


def _shipped_trigger(name: str, trigger: Trigger, file: str | None) -> list[str]:
    """Shipped trigger ``name``'s table: each of ``trigger``'s fields under its description, its
    prompt being orbit's own ``file``, if it names one, and a field it leaves unset as unset."""
    descriptions = _descriptions(Trigger)
    lines = [f"# [triggers.{name}]"]
    for key, value in trigger.model_dump(mode="json").items():
        lines += [f"# {descriptions[key]}", _trigger_field(key, value, file)]
    return lines


def _trigger_field(key: str, value: Any, file: str | None) -> str:
    """The commented-out line of a shipped trigger's field ``key``, at its ``value``: its prompt
    as orbit's own ``file``, if it names one, and a field it leaves unset as unset."""
    if key == "prompt" and file is not None:
        return f"# prompt: orbit's own {file}"
    if value is None:
        return f"# {key}: unset"
    return _key(key, value)


def _fitted(name: str, key: str, value: Any) -> str:
    """The commented-out line giving built-in role ``name``'s field ``key`` its ``value``, if it
    fits in :data:`REFERENCE_WIDTH` columns or isn't a list; otherwise how many the list holds,
    and the command that lists them."""
    line = _key(key, value)
    if len(line) <= REFERENCE_WIDTH or not isinstance(value, list):
        return line
    show = f"orbit config show roles.{name}.{key}"
    return f"# {key}: {len(value)} of orbit's own; {show} lists them"


if __name__ == "__main__":
    write_reference(Path(sys.argv[1]) if len(sys.argv) > 1 else REFERENCE_PATH)
