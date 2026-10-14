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
uv run orbit chat --no-review                  # don't review landed work
uv run orbit chat --no-worktree                # tasks edit the current checkout; nothing is committed
```

In the app:

| Key | Action |
|---|---|
| `h` `j` `k` `l` | Move between windows (the selected one is highlighted) |
| `i` | Jump to the prompt and start typing (vim-style insert mode) |
| `enter` | On the task list: activate it (`j`/`k` move, `enter` edits a task or adds one on `+ new task`, `a` accepts a draft, `x` removes a task, `esc` leaves) |
| `ctrl+t` | Switch the prompt between the conversation and the planner |
| `f` | Show only one agent's part of the conversation; press again for the next agent, then everything |
| `enter` | On the agents pane: activate it (`j`/`k` move, `c` cancels the highlighted agent's work, `esc` leaves) |
| `ctrl+r` | Run the tasks that haven't completed yet |
| `ctrl+g` | Ask for a super review of the whole codebase now (works while tasks run) |
| `ctrl+o` | Pick the model (default, fable, opus, sonnet, haiku) |
| `ctrl+l` | Pick the tool access level (off, restricted, full) |
| `esc` | Stop typing; otherwise cancel the running turn or close a picker |
| `ctrl+c` | Quit |

In a picker, `j`/`k` (or the arrow keys) move and `enter` chooses.

### Tasks

The task list queues prompts to run later. Each task has its own prompt, model and tool access,
set in the task editor: `j`/`k` pick a field, `i` types in the prompt (`esc` or `enter` stops),
`enter` on model or tools opens a picker, `ctrl+s` saves and `esc` discards.

`ctrl+r` runs the tasks one after another, each through a fresh **agent**: a new `claude -p`
session connected to orbit's MCP server, so tasks don't share context with each other or with the
conversation. The agent is told only who it is (`agent-1`, `agent-2`, …) and to fetch its task:

1. It calls orbit's `get_task` tool and gets the task's prompt.
2. When it thinks it's done, it calls `complete_task` with a one-line summary. Orbit runs the
   verification steps while that call waits, and replies with the result.
3. If a step failed, the reply quotes the end of its output and asks the same agent to fix the
   cause without weakening the check, then call `complete_task` again. The row shows `fix 1/5`,
   `fix 2/5`, …
4. Once verification passes, the agent is told to stop. It gets a few seconds to exit on its own
   before orbit stops it.

An agent may also run a single step itself with `run_check` (up to 20 times per task), note what
it's doing with `report_progress`, or call `give_up` with a reason when the task can't be done.

The **agents pane** (bottom right) lists the agents running now: each one's role, its task, and
whether it's working or waiting on orbit (say, `waiting on complete_task` while its work is
verified). Its subtitle says whether review is active or paused. Activate it with `enter` and press
`c` on an agent to cancel its work: for a reviewer, just that review (the next one starts); for the
coder or the planner, the same as `esc`.

The conversation shows everything in one timeline: each agent's own messages and tool calls,
prefixed with its id, and its exchanges with orbit (`agent-1 → orbit: complete_task …`,
`orbit → agent-1: …`) in their own colour; `f` narrows it to one agent at a time.

Each row of the task list shows its status: ⏳ pending, ◜ running (spinning), 🔍 verifying, ✅ done,
❗ failed verification, ❌ failed, ⌛ ran out of steps, 🏳 the agent gave up, 🛑 cancelled, 📝 a draft
the planner proposed. A failed task doesn't stop the rest; `esc` cancels the running task (stopping
its agent) and skips the remaining ones. Running again skips tasks that completed, failed
verification or were given up on, and editing a task makes it pending again. Tasks added while a
run is going join it; the running task can't be edited.

Edge cases:

- An agent that ends its turn without calling `complete_task` is taken to have finished, and its
  work is verified. If it stops after being asked for a fix without changing anything, it isn't
  verified again; the task falls back straight away (see below).
- An agent that exits without ever calling `get_task`, or can't reach orbit's MCP server, fails the
  task with an error.
- Calls out of order (claiming done before fetching the task, or twice while verification runs) get
  an error reply and change nothing.

### Verification steps

When an agent claims its task is done, orbit runs the verification steps in the working directory,
in order, stopping at the first failure. While that happens the task shows 🔍, the conversation notes
each step as it starts and ends, and the panel under the task list shows each step's progress on the
current task (⏳ pending, ◜ running (spinning) with its elapsed time, ✅ passed, ❌ failed,
🛑 cancelled, ⏩ skipped). There are six, quick ones first so a failure shows up in seconds; each must
exit successfully:

1. Ruff lint: `uv run ruff check`
2. Ruff format: `uv run ruff format --check`
3. Type check (mypy): `uv run mypy`
4. Import contracts: `uv run lint-imports`
5. Code quality (Lizard): `uv run lizard src tests -w -C 6 -L 100 -a 5`. Every function, tests
   included, must have a cyclomatic complexity of at most 6, at most 100 lines and at most 5
   parameters (`LIZARD_MAX_*` in `src/orbit/agent/external/checks.py`).
6. Unit tests, 100% coverage: `uv run pytest --cov --cov-fail-under=100`

Failures go back to the same agent, which keeps the context of what it just did. After
`fix_attempts` fixes (default 5, set in `.orbit/roles.toml`), or if the agent has exited, the task
**falls back**: it is marked ❗ and a fix task is inserted right after it, indented and numbered under
it (`1.1`, `1.2`, …), with the same model and tool access, for a fresh agent. A task's chain of
fallbacks gets up to 3 per step; once a step has used its 3, orbit reports the failure and moves on
to the next task. Once a fix passes, the task it fixed turns ✅, along with any earlier fixes in the
chain.

Verification is skipped for tasks run with tools `off`, for tasks that changed no files, and for an
agent that stopped without changing anything since its last failed check (that one falls back
instead). Orbit compares the files git sees (tracked, plus untracked ones that aren't ignored, by
size and modification time). Outside a git work tree it can't tell, so it verifies. A skipped task
still counts as done; the panel shows ⏩ for each step and the conversation says why.

Fixes need to edit files, so give tasks `full` tool access when you want them fixed automatically.
With `restricted` access, an agent can explain the failure but can't fix it.

### Where tasks run, and how their work lands

Inside a git repository, agents don't touch your checkout. They work in orbit's own worktree,
`.git/orbit/coder` on the branch `orbit/coder` (inside `.git`, so your checkout never sees it), and
the verification steps run there too. Before each task the worktree catches up with the branch you
had checked out when orbit started (the **base**), and `uv sync --frozen` runs in it whenever its
`uv.lock` changes.

When a task passes verification, orbit commits its work there (the agent's summary as the subject,
the task as the body) and fast-forwards the base to it, so the commit appears on your branch and in
your checkout (`⇢ Merged into the base branch.`). If you committed to the base in the meantime, the
work is rebased onto your commits and verified again before merging; if that fails, a fresh agent
fixes it. When it can't be merged, it stays committed on `orbit/coder` and the conversation says why
(`⚠ Not merged …`); orbit tries again after the next task:

- `conflict`: it conflicts with your commits. Merge `orbit/coder` yourself.
- `blocked`: uncommitted changes in your checkout are in the way. Commit or stash them.
- `detached`: you started orbit on a detached HEAD, so there's no branch to merge into.

Work is never thrown away. When a new task starts, changes a failed task left in the worktree are
stashed (`git stash list` shows them as `orbit: unverified work …`); a fallback fix carries on from
the work it fixes instead. At startup orbit mentions unmerged commits on `orbit/coder` and stashes
it left behind. Outside a git repository, or with `--no-worktree`, agents work in the current
directory and nothing is committed.

With `full` tool access an agent can still run commands outside its worktree; the worktree keeps
your checkout out of its way, it doesn't sandbox it.

### Planning with the planner

`ctrl+t` switches the prompt to the **planner** (its title reads `Prompt · planner`), and back. Tell it
what you want to build; it asks what's unclear and breaks the goal into small tasks, proposing each
with `propose_task`. Proposals appear in the task list as 📝 drafts, with the `coder` role's default
settings, and never run on their own: on the task list, `a` accepts the highlighted draft (or edit
it with `enter` and save, which accepts it too), and `x` removes it. Accepted tasks run with the
rest when you press `ctrl+r`.

The planner is one agent for the whole session: each prompt resumes its conversation, so it
remembers what you discussed. It reads your checkout but can't edit it, uses the model you picked
with `ctrl+o`, and its conversation is separate from the plain one.

### Reviews

Every commit from one of your tasks is reviewed in the background while the next task runs. Orbit
queues a `reviewer` task (it shows in the task list) carrying the commit's diff, and runs it in a
read-only snapshot of that commit. The reviewer suggests follow-ups with `suggest_task`: each becomes
a task at the end of the queue (with the `coder` role's default settings), and once the review ends
orbit runs them, unless you pressed `esc` since your last `ctrl+r`.

Suggestions from one review form a **batch**. When every task in a batch has finished, the batch's
commits are reviewed together, which may suggest more. To keep this from going on forever, each
batch is a round further from your own work, and once the batches reach `review_rounds` (default 3)
review pauses, with a note in the conversation; it resumes with the next commit from one of your
tasks. A review may suggest at most `review_suggestions` tasks (default 5), and a suggestion that
an unfinished task already covers is refused. With the defaults, one of your commits can lead to at
most 15 suggested tasks.

Every `super_review_every` commits (default 10, counting every commit orbit lands), a
`super-reviewer` (Opus by default) reviews the **whole codebase** at the latest commit, looking for
duplication, structure that no longer fits and gaps in tests, and may suggest up to
`super_review_suggestions` tasks (default 10). Those form a batch at round 1 and carry on like any
other review's. `ctrl+g` asks for one straight away and restarts the count. A super review that
falls due while review is paused waits until it resumes.

Reviews need the worktree; `--no-review` turns them off.

### Roles and `.orbit/roles.toml`

Every task names a **role**, the kind of agent that takes it (`coder` unless it says otherwise).
A role sets what the agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and an optional time limit. Its model and tool access are the
defaults for tasks orbit queues in that role; a task you queue keeps its own settings. Built in:

| Role | Tools | Verified | Default model / access |
|---|---|---|---|
| `coder` | `get_task`, `complete_task`, `run_check`, `report_progress`, `give_up` | yes | default / `full` |
| `reviewer` | `get_task`, `complete_task`, `report_progress`, `give_up` | no | default / `restricted` |
| `super-reviewer` | same as `reviewer` | no | `opus` / `restricted` |
| `planner` | same as `reviewer` | no | default / `restricted` |

An agent calling a tool its role doesn't have gets an error reply (the CLI only allows its role's
tools, too); an agent still working when its role's time runs out is stopped and the task fails.

A project can change these, and orbit's limits, in `.orbit/roles.toml`. A role it names that's built
in keeps the fields it doesn't set; any other name adds a role. Every field is optional:

```toml
fix_attempts = 5               # fixes asked of one agent before a fresh one takes over
review_rounds = 3              # rounds of review-suggested work before review pauses
super_review_every = 10        # commits between super reviews
review_suggestions = 5         # tasks one review may suggest
super_review_suggestions = 10  # tasks one super review may suggest

[roles.coder]
model = "sonnet"
timeout_minutes = 45

[roles.docs-writer]
instructions = "You write and update the project's documentation."
tools = ["get_task", "complete_task", "report_progress", "give_up"]
tool_access = "full"
verify = false
```

`orbit chat` reads it at startup and refuses to start if it's invalid, naming each problem (for
example `roles.coder.tool_access: Input should be 'off', 'restricted' or 'full'`).

### How agents reach orbit

Orbit starts its MCP server the first time a task runs, on a random port on `127.0.0.1`. Each agent
gets its own unguessable token, sent as `Authorization: Bearer …` with every call; orbit ignores calls
whose token it didn't issue, and revokes a token when its agent's run ends. The token is passed to
the CLI in the `ORBIT_AGENT_TOKEN` environment variable, which the MCP config references, so it never
appears in the process list. Agents are started with `--strict-mcp-config` (only orbit's server),
`--allowedTools 'mcp__orbit__*'` (orbit's tools are allowed at every access level), and
`ENABLE_TOOL_SEARCH=false` (orbit's tools are loaded up front).

Changes apply from the next prompt and keep the conversation.

Tool access levels:

- `off`: no tools (agents still have orbit's tools).
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
- `orbit.agent`: backend, with no UI imports. Three layers, each importing only the ones below:
  - `external`: adapters that reach outside the backend. `ClaudeCliRunner` streams `claude -p
    --output-format stream-json`, resumes the same Claude session across turns, and launches
    agents connected to orbit (`run_agent`, and `converse` for the planner); `McpHost` is orbit's
    MCP server; `GitCoderTree` is the coder's worktree and its snapshots; `config_file` reads
    `.orbit/roles.toml`; `checks` runs the verification steps as processes (`DEFAULT_STEPS`,
    `run_step`); `workspace` fingerprints the git working tree; `EchoModel` is a stand-in model.
  - `application`: `AgentSession` implements `Session`: it takes commands, asks the domain what
    follows, drives the ports and publishes events. `AgentRun` sees one task through one agent and
    `AgentDesk` routes agents' calls by token; `Verifier` runs the steps; `Lander` commits and
    merges verified work; `ReviewLane` runs reviews beside the work; `PlannerChat` is the
    planner's conversation. `ports` defines what it needs from outside (`AgentHost`,
    `AgentLauncher`, `CoderTree`, `TurnRunner`, `StepRunner`, `Workspace`, `ModelClient`, `Tool`);
    `ModelLoopRunner` is an in-house model/tool loop for direct API use.
  - `domain`: the rules, with no I/O: the `TaskBoard` (task order, statuses, fix chains),
    `AgentTask` (what orbit replies to an agent, and when its work is verified), the
    `AgentRegistry` (agent ids and tokens), `roles` (built-in roles and orbit's limits),
    `ReviewPlanner` (when to review, batches, rounds and caps), commit messages, when a task needs
    verifying, how many fixes to try per step, and the fix prompt.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`.
  `view` turns state and events into text and doesn't import Textual; the widgets (`tasks`,
  `agents`, `widgets`) lay it out.
- `orbit.cli`: the composition root, and the only place the two sides are wired together: it
  plugs the external adapters into `AgentSession` and hands that to the TUI.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
