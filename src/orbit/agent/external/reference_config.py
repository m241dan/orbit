"""Orbit's reference project configuration: every setting a project's ``.orbit/config.toml`` can
change, commented out at its default under a line saying what it does, generated from orbit's
models and the built-in roles shipped with it.

Run as ``python -m orbit.agent.external.reference_config [path]`` to write it, by default to
``REFERENCE_PATH``, the copy shipped with orbit.
"""

import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, TypeAdapter

from orbit.agent.domain.checks import Setup, VerificationConfig
from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig, load_config
from orbit.agent.domain.config_update import FOR_EXAMPLE
from orbit.agent.domain.roles import Role
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files

REFERENCE_PATH = Path("src/orbit/agent/external/reference/config.toml")
"""Where the reference is written, relative to the current directory: the copy shipped with
orbit."""

HEADER = (
    "orbit's project configuration, every setting commented out at its default.",
    "Uncomment the settings you change; the others keep their defaults.",
)

EXAMPLE_STEP = VerificationStep("Tests", ("uv", "run", "pytest"))
EXAMPLE_SETUP = Setup(command=("uv", "sync"), watch=("uv.lock",))
EXAMPLE_ROLE = "security-reviewer"
EXAMPLE_ROLE_FIELDS = Role(
    instructions="roles/security-reviewer.md",
    verify=False,
    lane="security",
    workspace="snapshot",
    merge=False,
)
"""The example of a role of a project's own: just the fields it sets."""

LATER_HEADING = "Coming in a later version of orbit; leave commented out until then."

LATER = (
    (
        "A trigger, which turns an event into a task for a role.",
        "[triggers.review-each-commit]",
        'The event it listens for, such as "task_landed", "commits", "manual" or "schedule".',
        'on = "task_landed"',
        "The events it takes, by their fields: here, work the coder landed.",
        'when = { role = "coder" }',
        "The role of the task it queues.",
        'role = "reviewer"',
        "The task's prompt, a template under .orbit/ that can use {commit}, {diff} and {task}.",
        'prompt = "triggers/review.md"',
        'The named rules that may hold it back: "review-rounds", "not-queued" or "dedupe".',
        'guards = ["review-rounds"]',
    ),
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
        "[roles.security-reviewer.tools]",
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
    verification step and a setup; each built-in role's table with every field at its shipped
    value; an example of a role of a project's own; and the settings of a later version of orbit
    under :data:`LATER_HEADING`. Everything is commented out, each key under its description.
    """
    roles = read_built_in_roles()
    files = read_instruction_files()
    config = _descriptions(OrbitConfig)
    sections = [
        _comments(HEADER),
        _limits(load_config({}, roles), config),
        _example(
            _descriptions(VerificationConfig)["steps"], "[[verification.steps]]", EXAMPLE_STEP
        ),
        _example(config["setup"], "[setup]", EXAMPLE_SETUP),
        _comments([config["roles"]]),
        *(_built_in(name, role, files[name]) for name, role in roles.items()),
        _example(
            "A role of your own, which must set its instructions.",
            f"[roles.{EXAMPLE_ROLE}]",
            EXAMPLE_ROLE_FIELDS,
        ),
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
    """The commented-out line giving ``key`` its ``value``, as TOML."""
    return f"# {key} = {tomlkit.item(value).as_string()}"


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
        lines += [f"# {descriptions[key]}", _role_field(key, value, file)]
    return lines


def _role_field(key: str, value: Any, file: str) -> str:
    """The commented-out line of a built-in role's field ``key``, at its ``value``: its
    instructions as orbit's own ``file``, and a field it leaves unset as unset, as TOML has no
    value for that."""
    if key == "instructions":
        return f"# instructions: orbit's own {file}"
    if value is None:
        return f"# {key}: unset"
    return _key(key, value)


if __name__ == "__main__":
    write_reference(Path(sys.argv[1]) if len(sys.argv) > 1 else REFERENCE_PATH)
