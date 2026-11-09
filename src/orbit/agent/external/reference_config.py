"""Orbit's reference project configuration: every setting a project's ``.orbit/config.toml`` can
change, commented out at its default under a line saying what it does, generated from orbit's
models and the built-in roles and triggers shipped with it.

Run as ``python -m orbit.agent.external.reference_config [path]`` to write it, by default to
``REFERENCE_PATH``, the copy shipped with orbit.
"""

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orbit.agent.domain.checks import Setup, VerificationConfig, VerificationSet
from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig, load_config
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import ROLE_TABLES, Role, RoleTools, ServerGrant
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.agent.external.built_in_triggers import read_built_in_triggers, read_template_files
from orbit.agent.external.toml_comments import (
    comments,
    described,
    example_table,
    field_descriptions,
    setting_line,
)

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
EXAMPLE_COMMAND_SERVER = McpServer(
    command=("github-mcp-server", "stdio"),
    env={"GITHUB_TOKEN": "$GITHUB_TOKEN"},
    shared=True,
    idle_minutes=10,
    timeout_seconds=300,
    approve=("create_issue",),
)
EXAMPLE_URL_SERVER = McpServer(
    url="http://127.0.0.1:3010/mcp", headers={"Authorization": "Bearer ${SEMGREP_TOKEN}"}
)
EXAMPLE_ROLE_SERVERS = RoleTools(
    mcp=(
        ServerGrant(server="semgrep"),
        ServerGrant(server="github", tools=("get_issue", "list_issues", "create_issue")),
    )
)
"""The examples of an MCP server orbit starts, of one already running at a URL, and of the role
of a project's own using them: all of one's tools, and some of the other's."""

LATER_HEADING = "Coming in a later version of orbit; leave commented out until then."

LATER = (
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
    table with every field at its shipped value, a list too long for a line in short, followed by
    its tools table; an example of a role of a project's own; each
    shipped trigger's table with every field at its shipped value; an example of skills by path;
    examples of an MCP server orbit starts, of one at a URL, and of the example role's tools
    using them; and the settings of a later version of orbit under :data:`LATER_HEADING`.
    Everything is commented out, each key under its description.
    """
    roles = read_built_in_roles()
    files = read_instruction_files()
    templates = read_template_files()
    config = field_descriptions(OrbitConfig)
    verification = field_descriptions(VerificationConfig)
    sections = [
        comments(HEADER),
        _limits(load_config({}, roles), config),
        example_table(verification["steps"], "[[verification.steps]]", EXAMPLE_STEP),
        example_table(
            verification["sets"], f"[verification.sets.{EXAMPLE_SET}]", EXAMPLE_SET_FIELDS
        ),
        example_table(config["setup"], "[setup]", EXAMPLE_SETUP),
        comments([config["roles"]]),
        *(
            section
            for name, role in roles.items()
            for section in _built_in(name, role, files[name])
        ),
        example_table(
            "A role of your own, which must set its instructions.",
            f"[roles.{EXAMPLE_ROLE}]",
            EXAMPLE_ROLE_FIELDS,
        ),
        comments([config["triggers"]]),
        *(
            _shipped_trigger(name, trigger, templates.get(name))
            for name, trigger in read_built_in_triggers().items()
        ),
        example_table(config["skills"], "[[skills]]", EXAMPLE_SKILLS),
        example_table(config["mcp"], "[mcp.github]", EXAMPLE_COMMAND_SERVER),
        example_table("An MCP server that's already running.", "[mcp.semgrep]", EXAMPLE_URL_SERVER),
        example_table(
            "The MCP servers a role's agents may use, in its tools table.",
            f"[roles.{EXAMPLE_ROLE}.tools]",
            EXAMPLE_ROLE_SERVERS,
        ),
        comments([LATER_HEADING]),
        *map(comments, LATER),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def write_reference(path: Path) -> None:
    """Writes the reference config to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reference_config(), encoding="utf-8")


def _limits(defaults: OrbitConfig, descriptions: Mapping[str, str]) -> list[str]:
    """The limits at their ``defaults``, each under its line of ``descriptions``."""
    return described({key: getattr(defaults, key) for key in LIMIT_KEYS}, descriptions)


def _built_in(name: str, role: Role, file: str) -> list[list[str]]:
    """Built-in role ``name``'s tables: its own, each of ``role``'s fields under its description,
    its instructions being orbit's own ``file``; then each of its fields that's a table of its
    own, under its description, its fields under theirs."""
    descriptions = field_descriptions(Role)
    values = role.model_dump(mode="json")
    own = described(
        {key: value for key, value in values.items() if key not in ROLE_TABLES},
        descriptions,
        lambda key, value: _role_field(f"roles.{name}", key, value, file),
    )
    tables = [
        [
            f"# {descriptions[key]}",
            f"# [roles.{name}.{key}]",
            *_role_table(f"roles.{name}.{key}", model, values[key]),
        ]
        for key, model in ROLE_TABLES.items()
    ]
    return [[f"# [roles.{name}]", *own], *tables]


def _role_table(table: str, model: type[BaseModel], values: Mapping[str, Any]) -> list[str]:
    """The lines of a built-in role's ``table``, a ``model``, at its ``values``: each field under
    its description."""
    return described(
        values,
        field_descriptions(model),
        lambda key, value: _role_field(table, key, value, ""),
    )


def _role_field(table: str, key: str, value: Any, file: str) -> str:
    """The commented-out line of the field ``key`` of a built-in role's ``table``, at its
    ``value``: its instructions as orbit's own ``file``, and otherwise as :func:`_fitted` writes
    it."""
    if key == "instructions":
        return f"# instructions: orbit's own {file}"
    return _fitted(table, key, value)


def _shipped_trigger(name: str, trigger: Trigger, file: str | None) -> list[str]:
    """Shipped trigger ``name``'s table: each of ``trigger``'s fields under its description, its
    prompt being orbit's own ``file``, if it names one, and a field it leaves unset as unset."""
    return [
        f"# [triggers.{name}]",
        *described(
            trigger.model_dump(mode="json"),
            field_descriptions(Trigger),
            lambda key, value: _trigger_field(key, value, file),
        ),
    ]


def _trigger_field(key: str, value: Any, file: str | None) -> str:
    """The commented-out line of a shipped trigger's field ``key``, at its ``value``: its prompt
    as orbit's own ``file``, if it names one, and otherwise as :func:`setting_line` writes it."""
    if key == "prompt" and file is not None:
        return f"# prompt: orbit's own {file}"
    return setting_line(key, value)


def _fitted(table: str, key: str, value: Any) -> str:
    """The commented-out line giving the field ``key`` of a built-in role's ``table`` its
    ``value`` as :func:`setting_line` writes it, if that fits in :data:`REFERENCE_WIDTH` columns or
    it isn't a list; otherwise how many the list holds, and the command that lists them."""
    line = setting_line(key, value)
    if len(line) <= REFERENCE_WIDTH or not isinstance(value, list):
        return line
    show = f"orbit config show {table}.{key}"
    return f"# {key}: {len(value)} of orbit's own; {show} lists them"


if __name__ == "__main__":
    write_reference(Path(sys.argv[1]) if len(sys.argv) > 1 else REFERENCE_PATH)
