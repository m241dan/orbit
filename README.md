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
context with each other or with the conversation. Their output appears in the conversation and each
row shows its status: ⏳ pending, ◜ running (spinning), 🔍 verifying, ✅ done, ❗ failed verification,
❌ failed, ⌛ ran out of steps, 🛑 cancelled. A failed task doesn't stop the rest; `esc` cancels the
running task and skips the remaining ones. Running again skips tasks that already completed, and
editing a task makes it pending again. Tasks added while a run is going join it; the running task
can't be edited.

### Verification steps

After each task completes, orbit runs the verification steps in the working directory, in order,
stopping at the first failure. While that happens the task shows 🔍, the conversation notes each step
as it starts and ends, and the panel under the task list shows each step's progress on the current
task (⏳ pending, ◜ running (spinning) with its elapsed time, ✅ passed, ❌ failed, 🛑 cancelled,
⏩ skipped). There are six, quick ones first so a failure shows up in seconds; each must exit
successfully:

1. Ruff lint: `uv run ruff check`
2. Ruff format: `uv run ruff format --check`
3. Type check (mypy): `uv run mypy`
4. Import contracts: `uv run lint-imports`
5. Code quality (Lizard): `uv run lizard src tests -w -C 6 -L 100 -a 5`. Every function, tests
   included, must have a cyclomatic complexity of at most 6, at most 100 lines and at most 5
   parameters (`LIZARD_MAX_*` in `src/orbit/agent/verify.py`).
6. Unit tests, 100% coverage: `uv run pytest --cov --cov-fail-under=100`

When a step fails, the task is marked ❗ and a fix task is inserted right after it, indented and
numbered under it (`1.1`, `1.2`, …), with the same model and tool access. Its prompt quotes the end
of the step's output and asks Claude to fix the cause without weakening the check. If the fix also
fails verification, another fix is queued. A task's chain of fixes gets up to 3 attempts per step,
so a task broken in several ways is fixed one step after another; once a step has used its 3, orbit
reports the failure and moves on to the next task. Once a fix passes, the task it fixed turns ✅,
along with any earlier fixes in the chain. Running again skips ❗ tasks, because their fix tasks
follow them. Tasks that errored or were cancelled aren't verified, and neither is the conversation.

Verification is also skipped for tasks run with tools `off`, and for tasks that changed no files:
orbit compares the files git sees (tracked, plus untracked ones that aren't ignored, by size and
modification time) before and after the task. Outside a git work tree it can't tell, so it verifies.
A skipped task still counts as done; the panel shows ⏩ for each step and the conversation says why.

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
uv run lizard src tests -w -C 6 -L 100 -a 5   # code quality: complexity and size per function
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
