# Orbit

Orbit is a TUI (Textual, `orbit.tui`) over a backend (`orbit.agent`) that runs Claude Code agents
through its own MCP server, in git worktrees, and verifies their work. README.md describes how it
works; keep it current: a change users can see (a key, a flag, a config setting, a role) updates
README.md in the same change.

## Checks

`.orbit/config.toml` lists the checks every change must pass, and `pyproject.toml` configures
them: ruff, strict mypy over `src` and `tests`, the import contracts (`lint-imports`), lizard and
pytest with 100% branch coverage. Run everything through uv, such as `uv run pytest
tests/test_roles.py`.

- Tests come first, and every line and branch is covered by a test that checks its behaviour.
- Tests are fully typed, like the code.
- Lizard holds every function, tests included, to cyclomatic complexity 6, 100 lines and 5
  parameters. Split a function well before it reaches them.

## Architecture

The import contracts in `pyproject.toml` keep this shape:

- `orbit.agent` (backend) and `orbit.tui` (frontend) are independent. They meet only in
  `orbit.cli`, through `orbit.protocol`, which imports neither.
- The backend is layered `external` > `application` > `domain`: a layer imports only the ones
  below it. The domain is pure rules, with I/O left to the layers above; when a rule needs data
  from outside, such as a file's contents, the caller reads it and passes it in.
- `orbit.agent.external` is the only place that uses mcp, uvicorn or starlette.
- `orbit.tui.view` holds the presentation rules as plain functions; the Textual widgets lay out
  what it returns.

The contracts catch imports, not responsibilities, so also keep logic where it belongs: rules in
the domain rather than in adapters, orchestration in the application, presentation in
`orbit.tui.view`.

## Conventions

- Models are frozen pydantic models or dataclasses.
- Docstrings are short plain English saying what a function returns or does ("The tasks that
  …"), with parameters in ``double backticks``. Comments are rare and explain why.
- Async code awaits or cancels every task it starts, answers every held MCP call, rechecks state
  that may have changed across an `await`, and cleans up when cancelled.
- Agents and git are handled safely: tokens are revoked when an agent's run ends, worktrees and
  branches are left in a good state, work survives a failed merge, and the user's own checkout is
  only touched when that's the point.
