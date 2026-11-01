# Orbit

Orbit is a TUI (Textual, `orbit.tui`) over a backend (`orbit.agent`) that runs coding agents
through its own MCP server, in git worktrees, and verifies their work. README.md describes how
it works.

Orbit's conventions live in skills, which orbit gives its own agents in their instructions.
Before changing code in a session outside orbit, read these, and the files their sections
point to:

- `src/orbit/agent/external/skills/code-style/SKILL.md`: how code is built here.
- `src/orbit/agent/external/skills/python/SKILL.md`: the Python tools and conventions.
- `src/orbit/agent/external/skills/application/SKILL.md`: what an application's code types
  hold.
- `.orbit/skills/layers/SKILL.md`: where each code type lives in Orbit, and the import
  contracts.
- `.orbit/skills/orbit/SKILL.md`: Orbit's own rules for agents, git, modes, the protocol,
  telemetry and tests.

`.orbit/config.toml` lists the checks every change must pass. Run everything through uv, such
as `uv run pytest tests/test_roles.py`.
