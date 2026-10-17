# orbit

Orbit command-line tool.

## Install

Requires [uv](https://docs.astral.sh/uv/) and the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`) on your `PATH`.

Install orbit once, from a clone or straight from git, and use it on any project:

```sh
uv tool install /path/to/orbit        # or: uv tool install git+<repository-url>
cd ~/code/project && orbit chat       # work on the project you're in
orbit chat -C ~/code/other            # or point it at another one
```

`-C`/`--workspace` names the project (the current directory by default). Inside a git repository,
orbit works from its top level, so a subdirectory picks the whole repository; the conversation
notes the workspace at startup. Each project brings its own verification steps, worktree setup,
roles and limits in its `.orbit/config.toml` (see [Verification steps](#verification-steps) and
[Roles and `.orbit/config.toml`](#roles-and-orbitconfigtoml)); without one, nothing is verified and
no setup runs.

To develop orbit itself, work in a clone and run it with `uv run`:

```sh
uv sync
uv run pre-commit install
```

## Usage

```sh
uv run orbit chat                              # interactive agent session
uv run orbit chat -C ~/code/other              # work on another project
uv run orbit chat --model opus --tools full    # pick the planner's model and new tasks' tool access
uv run orbit chat --no-verify                  # don't verify tasks (ctrl+e is refused)
uv run orbit chat --no-review                  # don't review landed work
uv run orbit chat --no-worktree                # tasks edit the current checkout; nothing is committed
uv run orbit chat --no-usage                   # don't show usage limits
uv run orbit chat --profile /tmp/orbit-prof    # record what slows orbit down
```

With orbit installed as a tool, drop the `uv run`.

`--profile <dir>` records where time goes, for when orbit bogs down. It writes `ui.prof`, a
profile that `python -m pstats` or `snakeviz` opens, and `ui-blocked.log` and
`backend-blocked.log`, with an entry for every time the UI's or the backend's event loop was
blocked for over 50 ms showing what its thread was running while it was stuck, and how long it
lasted. Python allows only one profile at a time, and it sees every thread, so `ui.prof` covers
the backend too and there's no `backend.prof`.

In the app:

| Key | Action |
|---|---|
| `h` `j` `k` `l` | Move between windows (the selected one is highlighted) |
| `i` | Jump to the prompt, which talks to the planner, and start typing (vim-style insert mode) |
| `enter` | On the conversation: activate it (`j`/`k` move between entries, `enter` collapses or expands the one marked `▌` in the left gutter, `esc` leaves and holds the marked entry); again, resume at the held entry |
| `enter` | On the task list: activate it (`j`/`k` move, `enter` edits a task or adds one on `+ new task`, `a` accepts a draft, `x` removes a task, `esc` leaves) |
| `enter` | On the agents pane: activate it (`j`/`k` move, `c` cancels the highlighted agent's work, `esc` leaves) |
| `ctrl+t` | Jump to the **planner** tab; again, back to the tab you were on |
| `ctrl+r` | Run the tasks that haven't completed yet |
| `ctrl+g` | Ask for a super review of the whole codebase now (works while tasks run) |
| `ctrl+e` | Set up (or redo) the verification steps: after you confirm (`y` to go ahead, `n`/`esc` to cancel), queue a configurator task first and run it (see [Setting up verification](#setting-up-verification)) |
| `ctrl+o` | Pick the planner's model (default, fable, opus, sonnet, haiku; new tasks default to it too) |
| `ctrl+l` | Pick the tool access level new tasks start with in the task editor (off, restricted, full) |
| `esc` | Stop typing, leave a list or close a picker; with the conversation selected and nothing active, let go of its held entry and follow the latest output again |
| `ctrl+s` | Stop the task run (on any tab) |
| `ctrl+x` | Stop the planner's turn, leaving the task run going |
| `a` / `r` / `x` | On a proposal of checks: accept it, ask for changes (type them, `enter` sends, `esc` goes back), or end the task (see [Setting up verification](#setting-up-verification)) |
| `ctrl+c` | Quit |

In a picker, `j`/`k` (or the arrow keys) move and `enter` chooses.

### Tasks

The task list queues prompts to run later. Each task has its own prompt, model and tool access, set
in the task editor's four fields: **Title**, the prompt's first line, which is what the task list
shows; **Prompt**, the rest of it, in a box that scrolls; **Model** and **Tools**. `j`/`k` pick a
field, `J`/`K` scroll the prompt box one line at a time (whichever field is selected), `i` types in
the title or prompt (`esc` stops; `enter` stops in the title and adds a line in the prompt),
`enter` on model or tools opens a picker, `ctrl+s` saves (from anywhere, even while typing) and
`esc` discards. A task needs a title; the prompt may be empty.

`ctrl+r` runs the tasks one after another, each through a fresh **agent**: a new `claude -p`
session connected to orbit's MCP server, so tasks don't share context with each other or with the
planner. The agent is told only who it is (`agent-1`, `agent-2`, …) and to fetch its task:

1. It calls orbit's `get_task` tool and gets the task's prompt.
2. When it thinks it's done, it calls `complete_task` with a one-line summary. Orbit runs the
   verification steps while that call waits, and replies with the result.
3. If a step failed, the reply quotes the start and end of its output and asks the same agent to
   fix the cause without weakening the check, then call `complete_task` again. The row shows
   `fix 1/5`, `fix 2/5`, …
4. Once verification passes, the agent is told to stop. It gets a few seconds to exit on its own
   before orbit stops it.

`get_task`'s reply names the steps that will verify the agent's work. An agent may also run a
single step itself with `run_check` (up to 20 times per task), which replies with the step's
output if it failed and just its last line if it passed; note what it's doing with
`report_progress`; or call `give_up` with a reason when the task can't be done.

The **agents pane** (bottom right) lists the agents running now: each one's role, its task, and
whether it's working or waiting on orbit (say, `waiting on complete_task` while its work is
verified). Its subtitle says whether review is active or paused. Activate it with `enter` and press
`c` on an agent to cancel its work: for a reviewer, just that review (the next one starts); for the
planner, just its turn; for the coder, the same as `ctrl+s`.

The **Usage panel** sits in the right column, between the verification panel and the agents pane.
It stays hidden until a usage report arrives (orbit fetches it with the token in
`~/.claude/.credentials.json`; see [Usage limits](#usage-limits)). For each limit, such as Session
and Week, it shows a header with the percent used and the local reset time (`3:40pm` for today,
`Mon 9:00am` for another day), and a bar that is green below 75%, yellow from 75% and red from 90%.
When usage is rate limited or unavailable, it dims the last good numbers and adds a status line
(`usage: rate limited, retrying …` or `usage unavailable: …`); when there are no limits, it says
`no usage limits reported`. It's display only: it can't be focused, and `h`/`j`/`k`/`l` skip it.

The conversation shows everything in one timeline: each agent's own messages and tool calls,
prefixed with its id, and its exchanges with orbit (`agent-1 → orbit: complete_task …`,
`orbit → agent-1: …`) in their own colour. The tabs above it narrow it: **All** shows everything,
then come a pinned tab for each built-in role, labelled by its name: **planner**, **coder**,
**reviewer**, **super-reviewer** and **configurator**. The pinned tabs are there from the start and
keep the whole session's history; every agent in a role shares its tab (two reviewers running at
once both show under **reviewer**), each entry prefixed with its agent's id, so you can tell them
apart. Agents in any other role, such as the startup check (`checker`) or a custom role from
`.orbit/config.toml`, get a temporary tab of their own after the pinned ones, labelled by role
(`checker`, `checker 2`, …), which closes when the agent exits; their entries stay under All. A
role's tab also shows its tasks' headers (`Task 2: …`), verification results and merge notes, so a
coder task's whole run reads under **coder**; those of a task in a role without a pinned tab, and
the startup check's, show only under All, as do notices. Click a tab to switch, cycle with
`tab`/`shift+tab`, or jump to the planner tab with `ctrl+t` (again goes back). A role's tab starts
with `●` while one of its agents is running (the planner's while it's answering), and a tab with
output you haven't seen, a task note included, ends in `*`. Whichever tab is shown, the prompt goes
to the planner.

Each entry is coloured by what it is: your prompts, agents' messages, tool calls and their output,
verification and notices, and errors. Agents' and the planner's messages render as markdown:
headings, emphasis, lists, tables, highlighted code and links shown as `text (url)`. A tool call shows as one line naming the tool and its main
argument, such as the file or command, with its full arguments collapsed beneath it; an agent's
request to orbit shows the same way. An entry of more than one line can be collapsed to its first
line and a count of the rest, marked `▸` (`▾` when expanded); tool calls, their output and requests
to orbit start collapsed. Press `enter` on the conversation to move between entries with `j`/`k`, `enter` again to collapse or
expand the one marked `▌` in the left gutter, and `esc` to leave. Leaving holds the marked entry:
the mark stays, the conversation stops following new output, and `enter` picks up there again.
`esc` again with the Conversation window selected lets go, and the conversation follows the latest
output again. Each tab holds its own spot, kept while you switch tabs.

The planned improvements to the conversation window, and what is deferred, are in
[docs/conversation-window.md](docs/conversation-window.md).

Each row of the task list shows its status: ⏳ pending, ◜ running (spinning), 🔍 verifying, ✅ done,
❗ failed verification, ❌ failed, ⌛ ran out of steps, 🏳 the agent gave up, 🛑 cancelled, 📝 a draft
the planner proposed. A failed task doesn't stop the rest; `ctrl+s` cancels the running task (stopping
its agent) and skips the remaining ones. Running again skips tasks that completed, failed
verification or were given up on, and editing a task makes it pending again. Tasks added while a
run is going join it; the running task can't be edited.

Edge cases:

- An agent that ends its turn without calling `complete_task` is taken to have finished, and its
  work is verified. If it stops after being asked for a fix without changing anything, it isn't
  verified again; the task falls back straight away (see below).
- A task's agent that exits without ever calling `get_task`, or can't reach orbit's MCP server,
  fails the task with an error. (The planner runs no task, so it has no `get_task`.)
- Calls out of order (claiming done before fetching the task, or twice while verification runs) get
  an error reply and change nothing.

### Verification steps

When an agent claims its task is done, orbit runs the verification steps in the working directory,
in order, stopping at the first failure. While that happens the task shows 🔍, the conversation notes
each step as it starts and ends, and the panel under the task list shows each step's progress on the
current task (⏳ pending, ◜ running (spinning) with its elapsed time, ✅ passed, ❌ failed,
🛑 cancelled, ⏩ skipped). Each step is a command that must exit successfully.

The steps come from the project's `.orbit/config.toml`, one `[[verification.steps]]` table each, run
in the order listed. Each needs a non-empty `name` and `command` (the program and its arguments,
run without a shell):

```toml
[[verification.steps]]
name = "Ruff lint"
command = ["uv", "run", "ruff", "check"]
```

Orbit's own [`.orbit/config.toml`](.orbit/config.toml) lists six, quick ones first so a failure
shows up in seconds:

1. Ruff lint: `uv run ruff check`
2. Ruff format: `uv run ruff format --check`
3. Type check (mypy): `uv run mypy`
4. Import contracts: `uv run lint-imports`
5. Code quality (Lizard): `uv run lizard src tests -w -C 6 -L 100 -a 5`. Every function, tests
   included, must have a cyclomatic complexity of at most 6, at most 100 lines and at most 5
   parameters.
6. Unit tests, 100% coverage: `uv run pytest --cov --cov-fail-under=100`

**Startup check.** When orbit starts with verification on and at least one step configured, it
runs the steps once in the background against the base commit, so a broken setup shows up while
you type your first prompt rather than on the first task. It runs in a read-only snapshot of the
base (the same kind reviews use, under `.git/orbit`), set up with the project's setup command
first, so it never races with an agent in the worktree; outside a git repository it runs in the
current directory. Nothing waits for it: the prompt, the planner and `ctrl+r` all work meanwhile.
The verification panel shows its progress under a "Startup check" heading, with the same icons and
timing as a task's verification, until a task starts and its own verification takes the panel
over. It's listed in the agents pane as `startup-check`, where `c` cancels it. When it finishes,
the conversation notes `✅ Startup check passed`, or on a failure the step that failed (`setup`
if the setup command did) and its output, with a 📝 draft task "Fix failing check:
<step>" whose prompt holds that output, and a reminder that `ctrl+e` reconfigures the steps if
they're what's wrong. `--no-verify` skips it, as does having no steps.

With no steps configured, nothing is verified: the conversation says so at startup (`No verification
steps configured. Press ctrl+e to set them up.`), and each task that would have
been verified is skipped, with the reason in the conversation. `--no-verify` turns verification off
whatever the file says.

Failures go back to the same agent, which keeps the context of what it just did. After
`fix_attempts` in-session fixes (default 5, set in `.orbit/config.toml`), or if the agent has exited, the task
**falls back**: it is marked ❗ and a fix task is inserted right after it, indented and numbered under
it (`1.1`, `1.2`, …), with the same model and tool access, for a fresh agent. A task's chain of
fallbacks gets up to `fallback_fixes` per step (default 3, also set in `.orbit/config.toml`); once a
step has used them, orbit reports the failure and moves on to the next task. Once a fix passes, the task it fixed turns ✅, along with any earlier fixes in the
chain.

Verification is skipped for tasks run with tools `off`, for tasks that changed no files, and for an
agent that stopped without changing anything since its last failed check (that one falls back
instead). Orbit compares the files git sees (tracked, plus untracked ones that aren't ignored, by
size and modification time). Outside a git work tree it can't tell, so it verifies. A skipped task
still counts as done; the panel shows ⏩ for each step and the conversation says why.

Fixes need to edit files, so give tasks `full` tool access when you want them fixed automatically.
With `restricted` access, an agent can explain the failure but can't fix it.

### Setting up verification

A task in the `configurator` role works out a project's verification steps for you. Its agent
inspects the repository (CI workflows, `.pre-commit-config.yaml`, `package.json` scripts,
`pyproject.toml`, the Makefile and so on) and proposes the checks the project already uses, quick
ones first, plus a setup command and the files whose changes should rerun it. It doesn't edit
files.

Press `ctrl+e` to start one. It asks for confirmation first, so a stray keypress does nothing:
`y` goes ahead, `n` or `esc` cancels (`enter` doesn't answer). Once confirmed, the task goes to
the front of the task list and runs straight away (if tasks are already running, it runs
next). It works whether or not steps are configured: its prompt
lists the steps and setup configured now, if any, so the agent builds on them, which makes `ctrl+e`
the way to reconfigure too. While a configurator task is queued or running, a confirmed `ctrl+e`
doesn't add another; the conversation says `A configurator task is already queued or running.`
With `--no-verify` there's nothing to set up checks for, so a confirmed `ctrl+e` adds no task and
the conversation says `Setting up checks needs verification on; orbit was started with --no-verify.`

Orbit tries each proposal in the agent's working directory first: the setup, then each step in
order. If one fails, the agent gets the step's name and its output and is asked to revise,
without bothering you. Once everything passes, a window shows the proposal: the setup
command and the files it watches, then the steps in order, each with its name and command, and a
line saying they all passed on the current code. Then:

| Key | Action |
|---|---|
| `a` | Accept the proposal; the task completes |
| `r` | Ask for changes: type what should change, `enter` sends it to the agent, `esc` goes back to the proposal |
| `x` | End the task; it's marked 🛑 cancelled |

Your decision is noted in the conversation (`orbit → agent-3: proposal accepted`, `… changes
asked for: add mypy`, `… task ended`), followed by orbit's reply to the agent. The agent revises
up to `fix_attempts` times. If it's cancelled or exits while its proposal is showing, the window
closes and the task is cancelled.

Accepted checks take effect at once. Orbit itself (not the agent) writes them into
`.orbit/config.toml`, replacing its `[[verification.steps]]` and `[setup]` and keeping every other
key and comment, creating the file if it's missing. In the worktree the change is committed as
"Configure verification steps", with the steps listed in the body, and lands on the base like any
task's work; with `--no-worktree` the file is written in the current directory and not committed.
The next task is verified with the new steps, and its worktree set up with the new setup, without
a restart. Agents can't change their checks any other way: orbit reads the file only at startup.

### Where tasks run, and how their work lands

Inside a git repository, agents don't touch your checkout. They work in orbit's own worktree,
`.git/orbit/coder` on the branch `orbit/coder` (inside `.git`, so your checkout never sees it), and
the verification steps run there too. Before each task the worktree catches up with the branch you
had checked out when orbit started (the **base**), and the project's setup command runs in it: once
when the worktree is first prepared, and again whenever a file it watches changes. Set it in
`.orbit/config.toml` (without a `[setup]` table, nothing runs):

```toml
[setup]
command = ["uv", "sync", "--frozen"]
watch = ["uv.lock"]   # rerun when any of these files change
```

When a task passes verification, orbit commits its work there (the agent's summary as the subject,
the task as the body) and fast-forwards the base to it, so the commit appears on your branch and in
your checkout (`⇢ Merged into the base branch.`). If you committed to the base in the meantime, the
work is rebased onto your commits and, if its role is verified, verified again before merging; if
that fails, a fresh agent fixes it. When it can't be merged, it stays committed on `orbit/coder` and the conversation says why
(`⚠ Not merged …`); orbit tries again after the next task:

- `conflict`: it conflicts with your commits. Merge `orbit/coder` yourself.
- `blocked`: uncommitted changes in your checkout are in the way. Commit or stash them.
- `detached`: you started orbit on a detached HEAD, so there's no branch to merge into.

Work is never thrown away. When a new task starts, changes a failed task left in the worktree are
stashed (`git stash list` shows them as `orbit: unverified work …`); a fallback fix carries on from
the work it fixes instead. At startup orbit mentions unmerged commits on `orbit/coder` and stashes
it left behind, and snapshot worktrees in `.git/orbit` that an earlier run didn't remove (killed
mid-review, say). It leaves those for you to remove with `git worktree remove`, since another orbit
session on the repository may still be reading them. Outside a git repository, or with `--no-worktree`, agents work in the current
directory and nothing is committed.

With `full` tool access an agent can still run commands outside its worktree; the worktree keeps
your checkout out of its way, it doesn't sandbox it.

### Planning with the planner

The prompt talks to the planner (its title reads `Prompt · planner`); orbit is for planning, tasks
and reviews, with no free-form chat besides. The **planner** tab, always second in the strip, shows
just the planner conversation: your prompts, and the planner's messages, tool calls and exchanges
with orbit (`ctrl+t` jumps there, and pressed again goes back to the tab you were on). Tell it
what you want to build; it asks what's unclear and breaks the goal into small tasks, proposing each
with `propose_task`. Its first prompt, and the first after the verification steps change, tells it
the steps each task must pass. Proposals appear in the task list as 📝 drafts, with the `coder`
role's default settings, and never run on their own: on the task list, `a` accepts the highlighted
draft (or edit it with `enter` and save, which accepts it too), and `x` removes it. Accepted tasks
run with the rest when you press `ctrl+r`.

The planner answers in a lane of its own, so you can plan while tasks run (and press `ctrl+r`
while it answers); the prompt waits only for the planner's own reply. `ctrl+x` stops only the
planner's turn and leaves the task run going; `ctrl+s` stops the task run, whichever tab is shown. The
task list beside it fills with
drafts as the planner proposes them; when it says something while you're on another tab, the
planner tab gets a `*`.

The planner is one agent for the whole session: each prompt resumes its conversation, so it
remembers what you discussed. It reads your checkout and can search the web but can't edit your
checkout, and uses the model you picked with `ctrl+o`.

### Reviews

Every commit from one of your tasks is reviewed in the background while the next task runs. Orbit
queues a `reviewer` task (it shows in the task list) carrying the commit's diff, and runs it in a
read-only snapshot of that commit. The reviewer suggests follow-ups with `suggest_task`: each becomes
a task at the end of the queue (with the `coder` role's default settings), and once the review ends
orbit runs them, unless you pressed `ctrl+s` since your last `ctrl+r`.

Suggestions from one review form a **batch**. When every task in a batch has finished, the batch's
commits are reviewed together, which may suggest more. To keep this from going on forever, each
batch is a round further from your own work, and once the batches reach `review_rounds` (default 3)
review pauses, with a note in the conversation; it resumes with the next commit from one of your
tasks. A review may suggest at most `review_suggestions` tasks (default 5), and a suggestion that
an unfinished task already covers is refused. With the defaults, one of your commits can lead to at
most 15 suggested tasks. Removing a suggested task drops it from its batch, which is then reviewed
without it (or closed, if nothing in it landed); removing a queued review closes the batch it would
have suggested into.

Every `super_review_every` commits (default 10, counting every commit orbit lands), a
`super-reviewer` (Opus by default) reviews the **whole codebase** at the latest commit, looking for
duplication, structure that no longer fits and gaps in tests, and may suggest up to
`super_review_suggestions` tasks (default 10). Those form a batch at round 1 and carry on like any
other review's. `ctrl+g` asks for one straight away and restarts the count. A super review that
falls due while review is paused waits until it resumes.

The reviews orbit queued can't be edited: remove one instead. Reviews need the worktree;
`--no-review` turns them off.

### Roles and `.orbit/config.toml`

Every task names a **role**, the kind of agent that takes it (`coder` unless it says otherwise).
A role sets what the agent is told about its job, which of orbit's tools it may call, whether its
claims of being done are verified, and an optional time limit. Its model and tool access are the
defaults for tasks orbit queues in that role; a task you queue keeps its own settings. Built in:

| Role | Tools | Verified | Default model / access |
|---|---|---|---|
| `coder` | `get_task`, `complete_task`, `run_check`, `report_progress`, `give_up` | yes | default / `full` |
| `reviewer` | `get_task`, `complete_task`, `report_progress`, `give_up`, `suggest_task` | no | default / `restricted` |
| `super-reviewer` | `get_task`, `complete_task`, `report_progress`, `give_up`, `suggest_task` | no | `opus` / `restricted` |
| `planner` | `propose_task`, `report_progress` | no | default / `restricted` |
| `configurator` | `get_task`, `propose_checks`, `report_progress`, `give_up` | no | default / `restricted` |

What each built-in role is told, its instructions, is the Markdown file named for it in
[`src/orbit/agent/external/instructions/`](src/orbit/agent/external/instructions/) (for example
`coder.md`), shipped with the package and read when orbit starts. A project can still replace a
role's instructions with `instructions` in `.orbit/config.toml`.

An agent calling a tool its role doesn't have gets an error reply (the CLI only allows its role's
tools, too, and orbit's others are disallowed so the agent doesn't see them); an agent still
working when its role's time runs out is stopped and the task fails.

A project can change these, orbit's limits, its verification steps (see
[Verification steps](#verification-steps)) and its worktree setup (see
[Where tasks run, and how their work lands](#where-tasks-run-and-how-their-work-lands)), in
`.orbit/config.toml`. A role it names that's built in keeps the fields it doesn't set; any other
name adds a role. Every field is optional:

```toml
fix_attempts = 5               # in-session fixes asked of one agent before its task falls back
max_checks = 20                # checks one agent may run itself before claiming done
fallback_fixes = 3             # fresh-agent fix tasks a task's chain may queue per failing step
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
`--allowedTools` naming the role's orbit tools (allowed at every access level) and
`--disallowedTools` naming orbit's others,
`ENABLE_TOOL_SEARCH=false` (orbit's tools are loaded up front), and
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (your Claude Code auto-memory, which worktrees share with the
repository, stays out of agents' context).

A model picked with `ctrl+o` applies from the planner's next prompt and keeps its conversation; the
tool access picked with `ctrl+l` is what new tasks start with in the task editor. `ctrl+l` does not
change the planner's own tool access, which is the planner role's (`[roles.planner] tool_access` in
`.orbit/config.toml`, `restricted` by default).

Tool access levels:

- `off`: no built-in tools; agents have only orbit's tools.
- `restricted` (default): read-only. Agents start in Claude Code's `--restricted` mode, which
  removes Bash and the other tools that run code and ignores your user, project and local
  settings files; `Read`, `Glob`, `Grep` and `WebSearch` are allowed (read-only, including looking
  things up on the web), and `Edit`, `Write` and `NotebookEdit` are disallowed. `WebFetch` isn't
  allowed, as `--restricted` disables it. The project's `CLAUDE.md` still loads: `--restricted`
  skips it, so orbit names the agent's directory with `--add-dir` and sets
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`.
- `full`: every tool call is auto-approved (`--permission-mode bypassPermissions`). Claude can
  edit files and run commands without asking.

### Usage limits

The Usage panel in the right column shows **Session** (the 5-hour window) and **Week** (all
models): the percent used, a bar that turns yellow at 75% and red at 90%, and when each resets, in
local time.

Orbit fetches them from the same undocumented endpoint Claude Code's `/usage` uses, with your
Claude login's OAuth token from `~/.claude/.credentials.json` (Linux), sent only to
`api.anthropic.com`. It fetches at startup, every 5 minutes, and after an agent's turn ends (at most
once a minute). When it can't get them (no Claude login or an API-key login, an expired login, rate
limiting, network errors), the panel says why and greys out the last numbers; nothing else is
affected. The panel is display only.

`--no-usage` turns it off, and orbit then never reads the token.

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

- `orbit.protocol`: pydantic models (`Command`s in, `Event`s out), the `Session`
  interface, and `ThreadedSession`, which runs a session on a thread of its own. It imports no
  other orbit package.
- `orbit.agent`: backend, with no UI imports. Three layers, each importing only the ones below:
  - `external`: adapters that reach outside the backend. `ClaudeCliRunner` launches agents as
    `claude -p --output-format stream-json` runs connected to orbit (`run_agent`, and `converse`
    for the planner, which resumes its Claude session turn after turn); `McpHost` is orbit's
    MCP server; `GitCoderTree` is the coder's worktree and its snapshots; `config_file` reads
    `.orbit/config.toml`; `built_in_roles` reads the built-in roles' instructions from
    `instructions/*.md`; `checks` runs a verification step as a process (`run_step`);
    `processes` runs a program to completion without leaving it behind; `workspace`
    fingerprints the git working tree; `usage_api` fetches the Claude subscription's usage
    limits (`UsageApi`) with the token in `~/.claude/.credentials.json`.
  - `application`: `AgentSession` implements `Session`: it takes commands, asks the domain what
    follows, drives the ports and publishes events. `WorkLane` is the work lane: it runs the
    board's tasks one at a time, queues a fix after each failed step and lands work that passes.
    `AgentRun` sees one task through one agent; `AgentDesk` (in `desk`) launches agents and
    routes their calls by token; `Verifier` runs the steps; `Lander` (in `landing`) commits and
    merges verified work; `ReviewLane` runs reviews beside the work; `StartupCheck` runs the
    steps once on the base commit as orbit starts; `UsageMonitor` fetches the usage limits on a
    schedule and after an agent's turn; these three run their work through `Background` (in `lanes`), which
    reports a crash rather than losing it, and the session runs the work lane's and the
    planner's turns through a `TurnSlot` each (also in `lanes`), which refuses a second turn
    and shows the lane busy while one runs; `FrontendReview` is where the user decides on
    proposed checks; `PlannerChat` is the planner's conversation. `ports` defines what it needs
    from outside (`AgentHost`, `AgentLauncher`, `CoderTree`, `StepRunner`, `Workspace`,
    `ProposalReview`, `UsageSource`, `ConfigWriter`).
  - `domain`: the rules, with no I/O: the `TaskBoard` (task order, statuses, fix chains),
    `StepProgress` (each verification step's status, kept by the `Verifier` and the startup
    check), `AgentTask` (what orbit replies to an agent, and when its work is verified), the
    `AgentRegistry` (agent ids and tokens), `roles` (built-in roles, given their instructions,
    orbit's limits, its verification steps and worktree setup), `ReviewPlanner` (when to
    review, batches, rounds and caps), commit messages, when a task needs verifying, how many
    fixes to try per step, and the fix prompt.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`.
  `view` turns state and events into text and doesn't import Textual, with a module per area
  of the screen (`events`, `transcript`, `tool_calls`, `proposals`, `tasks`, `panels`, `agents`,
  `tabs`, and `settings` and `common` for what they share); the widgets (`transcript`,
  `tabs`, `tasks`, `agents`, `panels`, `proposals`, the proposal modal, and `widgets` for the
  building blocks they share) lay it out, and `app` is the Textual
  app that holds them.
- `orbit.cli`: the composition root, and the only place the two sides are wired together: it
  plugs the external adapters into `AgentSession` and hands that to the TUI. It runs the
  backend on its own thread and event loop through `orbit.protocol.ThreadedSession`, so neither
  side's work stalls the other.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
