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
uv run orbit chat --no-verify                  # don't verify tasks
```

In the app:

| Key | Action |
|---|---|
| `h` `j` `k` `l` | Move between windows (the selected one is highlighted) |
| `i` | Jump to the prompt and start typing (vim-style insert mode) |
| `enter` | On the task list: activate it (`j`/`k` move, `enter` edits a task or adds one on `+ new task`, `esc` leaves) |
| `ctrl+r` | Run the tasks that haven't completed yet |
| `ctrl+o` | Pick the model (default, fable, opus, sonnet, haiku) |
| `ctrl+l` | Pick the tool access level (off, restricted, full) |
| `esc` | Stop typing; otherwise cancel the running turn or close a picker |
| `ctrl+c` | Quit |

In a picker, `j`/`k` (or the arrow keys) move and `enter` chooses.

### Tasks

The task list queues prompts to run later. Each task has its own prompt, model and tool access,
set in the task editor: `j`/`k` pick a field, `i` types in the prompt (`esc` or `enter` stops),
`enter` on model or tools opens a picker, `ctrl+s` saves and `esc` discards.

`ctrl+r` runs the tasks one after another, each in a fresh Claude session, so they don't share
context with each other or with the conversation. Their output appears in the conversation and
each row shows its status: `○` pending, `▶` running, `✓` done, `✗` failed, `■` cancelled. A failed
task doesn't stop the rest; `esc` cancels the running task and skips the remaining ones. Running
again skips tasks that already completed, and editing a task makes it pending again.

### Verification steps

After each task completes, orbit runs the verification steps in the working directory, in order,
stopping at the first failure. The panel under the task list shows how each step did on the
current task. There is one step so far:

- Unit tests, 100% coverage: `uv run pytest --cov --cov-fail-under=100` must exit successfully.

When a step fails, the task is marked `⚠` and a fix task is inserted right after it, with the same
model and tool access. Its prompt quotes the end of the step's output and asks Claude to fix the
cause without weakening the check. If the fix also fails verification, another fix is queued, up
to 3 in a row; after that orbit reports the failure and moves on to the next task. Running again
skips `⚠` tasks, because their fix tasks follow them. Tasks that errored or were cancelled aren't
verified, and neither is the conversation.

Fix tasks need to edit files, so give tasks `full` tool access when you want them fixed
automatically. With `restricted` access, Claude can explain the failure but can't fix it.

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
