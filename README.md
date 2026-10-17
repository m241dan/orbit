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
uv run orbit chat --new                        # start a new session rather than picking one
uv run orbit chat --name "auth rework"         # start a new session with that name
uv run orbit chat --model opus --tools full    # planner's model, new tasks' access (over saved)
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
| `h` `j` `k` `l` | Move between windows (the selected one is highlighted); while the conversation's detail pane is open, `j` from the conversation selects the pane, and from the pane `k` goes back, `j` to the prompt and `l` to the task list |
| `i` | Jump to the prompt, which talks to the planner, and start typing (vim-style insert mode): long text wraps and the box grows up to 8 lines, then scrolls; `enter` sends, pasted text keeps its newlines, `esc` stops typing |
| `enter` | On the conversation: activate it (`j`/`k` move between entries, `enter` collapses or expands the one marked `▌` in the left gutter, or opens the detail pane on a tool call or request, `J`/`K` scroll the open detail pane, `y` copies its call's whole output, `tab` activates it, `esc` closes the detail pane if it's open, else leaves and holds the marked entry); again, resume at the held entry |
| `enter` | On the conversation's detail pane: activate it (`j`/`k` scroll it, `y` copies its call's whole output, `esc` leaves with the pane still open) |
| `enter` | On the task list: activate and hold it (`j`/`k` move, `enter` edits a task or adds one on `+ new task`, `a` accepts a draft, `x` removes a task, `esc` leaves, the list still held) |
| `enter` | On the agents pane: activate it (`j`/`k` move, `c` cancels the highlighted agent's work, `esc` leaves) |
| `t` | Hide agents' dim messages, still thinking out loud, in every tab; again, show them (works with the conversation activated too) |
| `ctrl+b` | Hide the sidebar so the conversation takes the full width (`h`/`j`/`k`/`l` skip its windows meanwhile); again, show it as it was (not while typing) |
| `ctrl+t` | Jump to the **planner** tab; again, back to the tab you were on |
| `ctrl+r` | Run the tasks that haven't completed yet |
| `ctrl+g` | Ask for a super review of the whole codebase now (works while tasks run) |
| `ctrl+e` | Set up (or redo) the verification steps: after you confirm (`y` to go ahead, `n`/`esc` to cancel), queue a configurator task first and run it (see [Setting up verification](#setting-up-verification)) |
| `ctrl+o` | Pick the planner's model (default, fable, opus, sonnet, haiku; new tasks default to it too) |
| `ctrl+l` | Pick the tool access level new tasks start with in the task editor (off, restricted, full) |
| `ctrl+p` | Switch sessions: open a saved one or start a new one (see [Sessions](#sessions)) |
| `esc` | Stop typing, close the conversation's detail pane, leave a list or close a picker; with the conversation selected and nothing active, let go of its held entry, closing the detail pane, and follow the latest output again; with the detail pane selected, close it; with the Task list selected and nothing active, let go of the held list and follow the active task again |
| `ctrl+s` | Stop the task run (on any tab) |
| `ctrl+x` | Stop the planner's turn, leaving the task run going |
| `ctrl+n` | Reset the planner: after you confirm (`y` to go ahead, `n`/`esc` to cancel), end its conversation so the next prompt starts a fresh one, with its role read again from `.orbit/config.toml` (see [Planning with the planner](#planning-with-the-planner)) |
| `a` / `r` / `x` | On a proposal of checks: accept it, ask for changes (type them, `enter` sends, `esc` goes back), or end the task (see [Setting up verification](#setting-up-verification)) |
| `ctrl+c` | Quit |

In a picker, `j`/`k` (or the arrow keys) move and `enter` chooses. In the list of sessions, `r`
renames the highlighted session and `x` deletes it (see [Sessions](#sessions)).

### Sessions

A session is what orbit is working on: its task list, its review batches and rounds (see
[Reviews](#reviews)), the planner's conversation, the planner's model and new tasks' tool access,
and its name and when it was created and last active. The count of commits towards the next super
review isn't a session's: it belongs to the repository, kept in `reviews.json` beside the sessions,
and carries on across sessions and restarts.

Orbit saves the open session after every change to `.git/orbit/sessions/` (`.orbit/sessions/`
outside a git repository; inside one, even with `--no-worktree`), one JSON file per session. Each
save replaces the file atomically, so a crash or `ctrl+c` leaves the last save whole. A new session
is saved once you add a task or prompt the planner; quitting before that leaves nothing behind.
Sessions are kept until you delete them: orbit never prunes them, and never overwrites a file it
can't read. The design, and what is deliberately left for later, is in
[docs/sessions.md](docs/sessions.md).

At startup orbit lists the saved sessions to pick from: `+ new session` first, then the sessions,
the most recently active first. Each row shows the session's name, when it was last active (local
time), how many of its tasks stand at each status, by the task list's icons (`3 ✅ 2 ⏳ 1 📝`), and
what they cost (`1.2M tok · $0.84`); `●` marks the open session. `j`/`k` (or the arrow keys) move
and `enter` opens the highlighted session. On `+ new session`, `enter` asks for a name: `enter`
starts the session, named for the date and time if you leave the name empty, and `esc` goes back to
the list. `esc` on the list quits orbit. Any draft orbit added before you picked, such as a fix for
a step that failed the startup check, carries over into the session you pick. `--new` skips the list
and starts a new session, and `--name NAME` starts one named `NAME`.

`ctrl+p` shows the same list later, to switch sessions; there `esc` closes it and keeps the open
session, as does picking the open session. Switching is refused, with an error saying what to stop,
while tasks run, a review runs or the planner answers. Opening a session never starts work by
itself: its pending tasks wait for `ctrl+r`. A task that was running or being verified when the
session was last saved (orbit crashed, say) comes back cancelled, and orbit says how many were
interrupted. An interrupted review ends as a cancelled one does: if its batch is then due a
review, one is queued for the next run. The planner carries on its saved conversation, and the session's saved model and tool
access come back, unless `--model` or `--tools` was given, which hold for this run.

Each session is locked while an orbit has it open: in another orbit's list it shows `open in
another orbit`, dimmed, and can't be opened. The lock is an OS lock on a `.lock` file beside the
session's, held while the orbit runs; when its process ends (orbit crashed, say) the lock goes with
it, and the next orbit to open the session takes it. A session file that can't be read shows `unreadable:`
and the reason, and can't be opened either.

In the list, `r` renames the highlighted session, the open one included: it asks for the new name,
starting from the old one; `enter` saves it (an empty name keeps the old one) and `esc` goes back.
Names needn't be unique. `x` deletes the highlighted session after a `y`/`n` confirm (`esc` is no):
its file goes, with any stale lock, while Claude's own conversation files stay. The open session
can't be deleted, and a session open in another orbit can be neither renamed nor deleted; an
unreadable one can be deleted but not renamed. The list refreshes after either. On `+ new session`,
`r` and `x` do nothing.

### Tasks

The task list queues prompts to run later. Each task has its own prompt, model and tool access, set
in the task editor's four fields: **Title**, the prompt's first line, which is what the task list
shows; **Prompt**, the rest of it, in a box that scrolls; **Model** and **Tools**. `j`/`k` pick a
field, `J`/`K` scroll the prompt box one line at a time (whichever field is selected), `i` types in
the title or prompt (`esc` stops; `enter` stops in the title and adds a line in the prompt),
`enter` on model or tools opens a picker, `ctrl+s` saves (from anywhere, even while typing) and
`esc` discards. A task needs a title; the prompt may be empty.

The task list belongs to the open session, which orbit saves as you go (see [Sessions](#sessions)).
It keeps the topmost running or verifying task in view, scrolling just enough to show it with the
row above it, and only when it would otherwise be out of view; activating the list with `enter`
starts the highlight on that task. Activating the list, or scrolling it with the mouse wheel,
holds it: it stops scrolling by itself and its title reads `Task list · held`. The first `esc`
leaves the list still held, with its highlight where it was; `esc` again, with the Task list
selected, lets go: the list scrolls to the running task at once and follows it again. Switching
sessions lets go too, leaving list mode if the list was active.
[docs/task-list.md](docs/task-list.md) records the requirements.

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

The conversation shows everything in one timeline: each agent's own messages, headed by the
agent's id in a muted row whenever the speaker changes, its tool calls, prefixed with its id, and
its requests to orbit and orbit's replies (`agent-1 → orbit complete_task … ✓ …`) in their own
colour. The tabs above it narrow it: **All** shows everything,
then come a pinned tab for each built-in role, labelled by its name: **planner**, **coder**,
**reviewer**, **super-reviewer** and **configurator**. The pinned tabs are there from the start and
keep the whole session's history; every agent in a role shares its tab (two reviewers running at
once both show under **reviewer**), each message headed and every other entry prefixed with its
agent's id, so you can tell them apart. Whether the speaker changed is judged within the tab: under
All, where agents interleave, messages are headed more often. The planner's messages are never
headed. Agents in any other role, such as the startup check (`checker`) or a custom role from
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
headings, emphasis, lists, tables, highlighted code and links shown as `text (url)`. They show dim
while the agent works, as it thinks out loud between tool calls; when its turn ends, however it
ends, even by the agent being stopped or crashing, its final answer (the messages after its last
tool call) brightens, under All and its tab. Your own prompts stand out: each starts with `❯`, its
following rows indented beneath it, on a faint band across the conversation's width, and renders as
markdown too; a prompt of more than 15 lines
starts collapsed (`❯ ▸ first line (+N lines)`), and an expanded one of several lines is marked
`❯ ▾`. Task headers (`Task 3: …`) stay bold in the accent colour. A tool call and its result are
one line, naming the tool and its main argument, such as the file or command, and updated in place:
a spinner turns in front while it runs (`◜ Read src/x.py`), and once it finishes it says what it
did, ticked `✓` or, in the error colour, crossed `✗`. A read counts its lines (`→ Read src/x.py ✓
12 lines`); an edit counts the lines it adds and removes, leaving out unchanged ones (`✓ +3 −1`,
summed over a MultiEdit's edits), and a write the lines it wrote (`✓ +40`); a command is ticked
alone (`✓`) and, if it fails, shows its exit code (`✗ exit 1`, or `✗` without one); a Grep or
Glob counts its matches (`✓ 4 matches`, `✓ 1 match`, `✓ 0 matches` when it finds nothing). Any
other tool, any other failure, and a call whose arguments don't fit count the output's lines
(`✓ 5 lines`, `✗ 1 line`). A call still running when its turn ends shows `→ Read src/x.py ⊘ no
result`. A tool call's line can't be expanded; `enter` on it shows its details instead (see
below). An agent's request to orbit and orbit's reply are one line too, naming the agent, orbit's
tool and its main argument, and updated in place: a spinner turns in front while the agent waits
(`◜ agent-3 → orbit complete_task Add a flag`), then the reply's first line follows, clipped to
about 60 characters, ticked (`agent-3 → orbit complete_task Add a flag ✓ All checks passed.`) or,
in the error colour, crossed (`✗ No task assigned.`). A request still waiting when its agent exits
shows `⊘ no reply`, muted. A reply to no request shown gets an entry of its own (`orbit →
agent-3: …`). Like a tool call's, a request's line can't be expanded; `enter` on it shows its
details. An entry of more than one line can be collapsed to its first line and a
count of the rest, marked `▸` (`▾` when expanded). Press `enter` on the conversation to move between entries with `j`/`k`, `enter` again to collapse or
expand the one marked `▌` in the left gutter, and `esc` to leave. Leaving holds the marked entry:
the mark stays, the conversation stops following new output, and `enter` picks up there again.
`esc` again with the Conversation window selected lets go, closing the detail pane if it's open, and
the conversation follows the latest output again. Each tab holds its own spot, kept while you switch tabs.

Every turn, the planner's and each agent's, ends with a muted rule across the conversation's width,
under All and the agent's tab: `── done · 1m12s · ↑1.2M ↓8k ──`. It says how the turn ended
(`done`, `stopped`, `out of steps`, or `error` in red, even when the error itself was already
shown), how long it took (`42s`, `1m12s`, `1h03m`), and, when the turn reports its usage, the
tokens it read (`↑`, input including cache writes and reads) and wrote (`↓`), rounded down
(`812`, `8.4k`, `14k`, `1.2M`). An agent's rule starts with its id (`── agent-3 done · 4m10s ──`);
the planner's doesn't.

An agent's run ends in exactly one rule, with no separate exit line. When the agent exits after
its last turn's rule, that rule stays, except that a `done` rule turns `stopped` if the agent was
killed, or `crashed` in red if it crashed. When it exits with no rule since its last turn started,
say crashing mid-turn, the exit draws its own: `── agent-3 crashed · 42s ──` in red, or `stopped`
or `done` muted, timed from its last turn's start (or the agent's, if no turn started), without
tokens. It lands under All and the agent's tab, a temporary tab included.

Press `t` (with nothing being typed) to hide agents' dim messages, so only final answers, tool
calls and everything else show, in every tab at once, tabs opened later included; the window's title
reads `Conversation · thinking hidden` while they're hidden, and `t` again shows them. A message that
brightens at the end of its turn appears. If the marked entry is hidden, the mark moves to the
nearest entry still shown, the one before it first. Agents' label rows follow the entries shown,
and the setting isn't kept across restarts.

A muted working row, pinned below the conversation, shows what runs right now: a line for each
agent whose turn has started and not yet ended, oldest turn first, such as
`◜ agent-3 · Bash uv run pytest 0:12 · 3:40`. That's the spinner, the agent (`planner` for the
planner), its latest tool call still running, clipped to fit (`Read src/x.py (+2 running)` when
others run alongside it), how long that call has run, and how long the turn has; an agent between
tool calls shows just its turn's time (`◜ agent-3 · 3:40`). Times read `0:12`, `3:40`, or
`1:02:05` from an hour. All shows every running agent, a role's tab only that role's agents, and
the planner tab only the planner. It shows at most three lines, then `+2 more`, can't be selected,
and takes no room while nothing in the shown tab runs.

`enter` on a tool call or a request to orbit, with the conversation activated, opens a detail pane
below the conversation and its working row, taking 40% of the conversation's height and titled
with the entry's line and a hint (`Detail · Read src/x.py · y copy`). It shows a header in the
form that suits the call's tool, a rule, then the output or orbit's reply:

- Read: the file's path (with `offset` and `limit` if given), then its lines highlighted for the
  file's type, with their line numbers; a file type it doesn't know stays plain.
- Bash: `$ ` and the whole command, then the output in its own colours.
- Grep and Glob: the pattern (and `in` the path, if given), then the matches as plain lines.
- Edit, MultiEdit and Write: the file's path, a rule, then the change as a diff: removed lines
  `-` in red, added lines `+` in green and unchanged ones dim, after a space. A MultiEdit shows
  each edit's diff, a dim `⋯` between them, and a Write shows all its content as added. A change
  with no lines, such as a Write of an empty file or a MultiEdit without edits, shows a dim `no
  lines changed` instead. Line
  numbers are best effort, right-aligned in a dim column before the lines: a Write's count from 1,
  and an edit's unchanged and added lines are numbered where its new text fits, in just one
  place, in the snippet of the file its result shows (its first line may end a line of the file
  and its last start one, or a single line sit inside one); removed lines, edits it can't place
  and failed calls have none. Then the output, dim, or red if the call failed.
- Every other tool: the arguments as highlighted JSON, then the output, indented and highlighted
  too if it's a JSON object or array.
- A request to orbit: the arguments as highlighted JSON, then `orbit: ` and the reply, red if it's
  an error.

A call whose arguments don't fit its tool's view shows them as JSON instead. A call still running
shows a dim `waiting for output…` until its result arrives, and the pane updates in place. Output,
or an edit's diff, of more than 200 lines shows its first 100, a dim `… N lines omitted · y copies
all …`, and its last 100, cut before it's highlighted. While the pane
is open it follows the cursor as `j`/`k` move, showing a dim `No details for this entry` on other
entries. The first `esc` closes it, staying in the list; the next leaves the list as usual.
Switching tabs closes it, and it stays closed when you come back; leaving the list any other way
than `tab` to the pane, such as activating the prompt, closes it too.

`J`/`K` scroll the open pane a line at a time from the conversation's list. To scroll it on its
own, `tab` from the list activates the pane, where `j`/`k` scroll it; `esc` leaves it selected and
still open, still following the conversation's held entry. While it's open, the pane is a window
of its own, between the conversation and the prompt: `j` from the conversation selects it, `enter`
activates it again, `k` goes back to the conversation, `j` on to the prompt and `l` to the task
list; the prompt's `k` still goes to the conversation. `esc` with the pane selected closes it.
Whenever the pane closes, the conversation is selected again. The pane starts at the top for each
entry it shows, and keeps its place as the same entry updates, such as when a running call
finishes. When the conversation is less than 20 rows high, the open pane covers all of it, the
entries and working row hidden, until it closes or the terminal grows; `j`/`k` still move
between entries underneath.

`y`, from the conversation's list or the activated pane while the pane is open, copies the whole
of its call to the clipboard: all of the output or orbit's reply, lines omitted from the pane
included, or the arguments as JSON while the call still runs or when it has no output; for an Edit, MultiEdit or Write,
the whole diff instead, unless it has no lines; a toast says `Copied N lines`. On
an entry without details it does nothing. Copying uses the terminal's clipboard escape (OSC 52),
which most terminals support; inside tmux it needs `set -g set-clipboard on`.

The planned improvements to the conversation window, and what is deferred, are in
[docs/conversation-window.md](docs/conversation-window.md).

Each row of the task list shows its status: ⏳ pending, ◜ running (spinning), 🔍 verifying, ✅ done,
❗ failed verification, ❌ failed, ⌛ ran out of steps, 🏳 the agent gave up, 🛑 cancelled, 📝 a draft
the planner proposed. Once a task has spent anything, its row ends with the tokens and dollars it
spent (`1.2M tok · $0.84`), and the task list's subtitle shows the open session's total
(`session: 1.2M tok · $0.84`): it starts from what the session's saved tasks spent when the session
opens (none for a new session, which shows no total) and adds every turn that finishes in this run,
the planner's included; token counts are rounded as in a turn's rule (see above). A failed
task doesn't stop the rest; `ctrl+s` cancels the running task (stopping
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
asked for: add mypy`, `… task ended`), and orbit's reply to the agent completes its request's
line above. The agent revises
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

The prompt talks to the planner (its title reads `Prompt · planner`). It wraps what you type and
grows with it up to 8 lines, then scrolls, and shrinks again once sent; `enter` sends, and pasted
text keeps its newlines. Orbit is for planning, tasks and reviews, with no free-form chat besides.
The **planner** tab, always second in the strip, shows just the planner conversation: your
prompts, and the planner's messages, tool calls and exchanges with orbit (`ctrl+t` jumps there,
and pressed again goes back to the tab you were on). Tell it what you want to build; it asks
what's unclear and breaks the goal into small tasks, proposing each with `propose_task` (a title
is required, and kept to one line). Its first prompt, and the first after the verification steps change, tells it
the steps each task must pass. Proposals appear in the task list as 📝 drafts, with the `coder`
role's default settings, and never run on their own: on the task list, `a` accepts the highlighted
draft (or edit it with `enter` and save, which accepts it too), and `x` removes it. Accepted tasks
run with the rest when you press `ctrl+r`.

The planner answers in a lane of its own, so you can plan while tasks run (and press `ctrl+r`
while it answers). While the planner answers, its tab shows `●` and the prompt's title reads
`Prompt · planner · busy`; you can still type in the prompt, but `enter` only says `Planner is
busy` and keeps your text, to send with `enter` once the planner is free. `ctrl+x` stops only the
planner's turn and leaves the task run going; `ctrl+s` stops the task run, whichever tab is shown. The
task list beside it fills with
drafts as the planner proposes them; when it says something while you're on another tab, the
planner tab gets a `*`.

The planner is one agent for the whole session: each prompt resumes its conversation, so it
remembers what you discussed. It reads your checkout and can search the web but can't edit your
checkout, and uses the model you picked with `ctrl+o`, which is saved with the session.

The planner's conversation is kept with its session, so it carries on across restarts and session
switches. `ctrl+n` resets it, after you confirm (`y` to go ahead, `n`/`esc` to cancel): to pick up a
change to the planner's role, or to clear a conversation grown too long. Orbit ends the planner's
conversation, and its access to orbit, so the next prompt starts a fresh one, told the
verification steps again, and saves the session without it. It reads `[roles.planner]` from
`.orbit/config.toml` again, so a change to the planner's instructions, tools or tool access takes
effect; other roles and settings stay as they were. If the file is invalid, the planner keeps its
role and orbit shows the problem, but still resets the conversation. Draft tasks stay on the task
list, the planner tab keeps its history, and the conversation shows a dim `── planner reset ──`
rule under the planner tab and All. The reset is refused while the planner answers (stop it with
`ctrl+x` first), and before its first prompt there's nothing to reset.

### Reviews

Every commit from one of your tasks is reviewed in the background while the next task runs. Orbit
queues a `reviewer` task (it shows in the task list) carrying the commit's diff, and runs it in a
read-only snapshot of that commit. The reviewer suggests follow-ups with `suggest_task` (a title
is required, and kept to one line): each becomes a task at the end of the queue (with the `coder` role's default settings), and once the review ends
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
other review's. `ctrl+g` asks for one straight away and restarts the count. The count belongs to
the repository rather than to a session, so it carries on across sessions and restarts. A super
review that falls due while review is paused waits until it resumes.

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
tool access picked with `ctrl+l` is what new tasks start with in the task editor. Both are saved
with the session. `ctrl+l` does not
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
    limits (`UsageApi`) with the token in `~/.claude/.credentials.json`; `session_files` keeps
    saved sessions (`SessionFiles`), one JSON file each with a lock file while an orbit has it
    open, and the repository's super review count, in the directory `state_dir` picks.
  - `application`: `AgentSession` implements `Session`: it takes commands, asks the domain what
    follows, drives the ports and publishes events. `Tasks` (in `tasks`) holds the `TaskBoard`,
    marks tasks running and finished, and is the one place that publishes the board, whole, once
    its callers have changed it. `WorkLane` is the work lane: it runs the board's tasks one at a
    time, queues a fix after each failed step and lands work that passes. `AgentRun` sees one
    task through one agent, and a `ProposalTrial` (in `proposal_trial`) tries the checks a
    configurator proposes and has the user decide on them; `AgentDesk` (in `desk`) launches
    agents and routes their calls by token; `Verifier` runs the steps; `Lander` (in `landing`)
    commits and merges verified work; `ReviewLane` runs reviews beside the work; `StartupCheck`
    runs the steps once on the base commit as orbit starts; `UsageMonitor` fetches the usage
    limits on a schedule and after an agent's turn; these three run their work through
    `Background` (in `lanes`), which reports a crash rather than losing it, and the session runs
    the work lane's and the planner's turns through a `TurnSlot` each (also in `lanes`), which
    refuses a second turn and shows the lane busy while one runs; `FrontendReview` is where the
    user decides on proposed checks; `PlannerChat` is the planner's conversation, running each
    turn and publishing how it ended; `SessionKeeper` (in `keeper`) is the open session: it
    saves it after every change once it's used, and lists, opens, renames, deletes and switches
    saved sessions through the store. `ports` defines what it needs from outside (`AgentHost`,
    `AgentLauncher`, `CoderTree`, `StepRunner`, `Workspace`, `ProposalReview`, `UsageSource`,
    `ConfigWriter`, `SessionStore`).
  - `domain`: the rules, with no I/O: the `TaskBoard` (task order, statuses, fix chains)
    and what a crash leaves of its tasks (`interrupted`),
    `StepProgress` (each verification step's status, kept by the `Verifier` and the startup
    check), `AgentTask` (what orbit replies to an agent, and when its work is verified), the
    `AgentRegistry` (agent ids and tokens), `roles` (built-in roles, given their instructions,
    orbit's limits, its verification steps and worktree setup), `ReviewPlanner` (when to
    review, batches, rounds and caps), commit messages, when a task needs verifying, how many
    fixes to try per step, and the fix prompt. `sessions` holds the rules of saved sessions:
    the `SessionRecord` and its format (versioned, with upgrades from older ones, or why a file
    is unreadable), how the list of sessions summarizes and orders them, when one may be
    opened, renamed or deleted, and the notes and settings a resumed session starts with.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`.
  `view` turns state and events into text and doesn't import Textual, with a module per area
  of the screen (`events`, `transcript`, `tool_calls`, `proposals`, `tasks`, `panels`, `agents`,
  `tabs`, `detail` and `detail_views` for the detail pane, `diffs` and `diff_numbers` for an
  edit's diff, `progress` for notes on a task's run, `rules` for the rules ending turns and
  agents' runs, `turns` for the conversation's running turns, `working` for the working row,
  `sessions` for the list of sessions, and `settings` and `common` for what they share); the
  widgets (`transcript`, `conversation` for the `ConversationFeed`, which turns events into the
  transcript's entries, `entries` for an entry and the renderables that draw it, `tabs`,
  `tasks`, `agents`, `panels`, `detail` for the detail pane, `working` for the working row,
  `proposals`, the proposal modal, `sessions`, the modal listing the sessions, and `widgets` for
  the building blocks they share) lay it out, and `app` is the Textual app that holds them.
- `orbit.cli`: the composition root, and the only place the two sides are wired together: it
  plugs the external adapters into `AgentSession` and hands that to the TUI. It runs the
  backend on its own thread and event loop through `orbit.protocol.ThreadedSession`, so neither
  side's work stalls the other.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
