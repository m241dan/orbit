# orbit

Orbit command-line tool.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`) on your `PATH`.

```sh
uv sync
uv run pre-commit install
```

## Usage

```sh
uv run orbit chat                              # interactive agent session
uv run orbit chat --model opus --tools full    # pick model and tool access up front
```

In the app:

| Key | Action |
|---|---|
| `h` `j` `k` `l` | Move between windows (the selected one is highlighted) |
| `i` | On the prompt: start typing (vim-style insert mode) |
| `ctrl+o` | Pick the model (default, fable, opus, sonnet, haiku) |
| `ctrl+l` | Pick the tool access level (off, restricted, full) |
| `esc` | Stop typing; otherwise cancel the running turn or close a picker |
| `ctrl+c` | Quit |

Changes apply from the next prompt and keep the conversation.

Tool access levels:

- `off`: no tools.
- `restricted` (default): Claude Code's normal permissions; anything that would need approval
  is refused, read-only actions still work.
- `full`: every tool call is auto-approved (`--permission-mode bypassPermissions`). Claude can
  edit files and run commands without asking.

## Development

Write tests first. Coverage is held at 100% (`fail_under` in `pyproject.toml`); only
untestable boilerplate is excluded (`__main__.py`, `assert_never` arms, `TYPE_CHECKING`).

```sh
uv run pytest --cov     # tests; fails below 100% coverage
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type-check
uv run lint-imports     # enforce frontend/backend separation
```

## Architecture

```
orbit.tui  ──Command──▶  orbit.protocol.Session  ──▶  orbit.agent
(Textual)  ◀──Event────                          ◀──  (agentic loop)
```

- `orbit.protocol`: pydantic models (`Command`s in, `Event`s out) and the `Session`
  interface. It imports no other orbit package.
- `orbit.agent`: backend. `AgentSession` implements `Session` and delegates each turn to a
  `TurnRunner`. `ClaudeCliRunner` (used by `orbit chat`) streams `claude -p --output-format
  stream-json` and resumes the same Claude session across turns; `ModelLoopRunner` is an in-house
  model/tool loop for direct API use. Has no UI imports.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`.
- `orbit.cli`: the only place the two sides are wired together.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
