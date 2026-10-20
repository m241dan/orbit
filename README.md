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
uv run orbit chat --state-dir /tmp/orbit-state # keep saved sessions somewhere else
uv run orbit db backup ~/orbit-backup.db       # back up the saved sessions (--force replaces)
uv run orbit serve                             # run the control plane on its own, for frontends
uv run orbit serve --port 0                    # on a port the system chooses
uv run orbit serve --tls tls --cert server.pem --key server-key.pem --host 0.0.0.0  # over TLS
uv run orbit chat --connect ws://127.0.0.1:7433/  # show the session orbit serve runs
uv run orbit chat --connect wss://orbit.example.lan:7433/ --ca ca.pem  # over TLS
uv run orbit chat --remote work                # connect to the control plane [remotes.work] names
uv run orbit chat --local                      # run in-process even with orbit serve running
uv run orbit token rotate                      # replace the token frontends show orbit serve
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
| `enter` | On the conversation: activate it (`j`/`k` move between entries, `k` on the first loading earlier ones, `enter` collapses or expands the one marked `▌` in the left gutter, or opens the detail pane on a tool call or request, `J`/`K` scroll the open detail pane, `y` copies its call's whole output, `tab` activates it, `esc` closes the detail pane if it's open, else leaves and holds the marked entry); again, resume at the held entry |
| `enter` | On the conversation's detail pane: activate it (`j`/`k` scroll it, `y` copies its call's whole output, `esc` leaves with the pane still open) |
| `enter` | On the task list: activate and hold it (`j`/`k` move, `enter` edits a task or adds one on `+ new task`, `a` accepts a draft, `x` removes a task, `f` shows why a task failed or stopped, `esc` leaves, the list still held) |
| `enter` | On the agents pane: activate it (`j`/`k` move, `c` cancels the highlighted agent's work, `esc` leaves) |
| `enter` | On the prompt, while viewing a session read-only: ask whether to make it the active session (see [Sessions](#sessions)) |
| `t` | Hide agents' dim messages, still thinking out loud, in every tab; again, show them (works with the conversation activated too) |
| `ctrl+b` | Hide the sidebar so the conversation takes the full width (`h`/`j`/`k`/`l` skip its windows meanwhile); again, show it as it was (not while typing) |
| `ctrl+t` | Jump to the **planner** tab; again, back to the tab you were on |
| `ctrl+r` | Run the tasks that haven't completed yet |
| `ctrl+g` | Ask for a super review of the whole codebase now (works while tasks run) |
| `ctrl+e` | Set up (or redo) the verification steps: after you confirm (`y` to go ahead, `n`/`esc` to cancel), queue a configurator task first and run it (see [Setting up verification](#setting-up-verification)) |
| `ctrl+o` | Pick the planner's model (default, fable, opus, sonnet, haiku; new tasks default to it too) |
| `ctrl+l` | Pick the tool access level new tasks start with in the task editor (off, restricted, full) |
| `ctrl+p` | Session picker: view or switch to a saved session, or start a new one (see [Sessions](#sessions)) |
| `esc` | Stop typing, close the conversation's detail pane, leave a list or close a picker; with the conversation selected and nothing active, let go of its held entry, closing the detail pane, and follow the latest output again; with the detail pane selected, close it; with the Task list selected and nothing active, let go of the held list and follow the active task again |
| `ctrl+s` | Stop the task run (on any tab) |
| `ctrl+x` | Stop the planner's turn, leaving the task run going |
| `ctrl+n` | Reset the planner: after you confirm (`y` to go ahead, `n`/`esc` to cancel), end its conversation so the next prompt starts a fresh one, with its role read again from `.orbit/config.toml` (see [Planning with the planner](#planning-with-the-planner)) |
| `enter` / `esc` | On a storage halt: try again, or close it (see [Storage failures](#storage-failures)) |
| `a` / `r` / `x` | On a proposal of checks: accept it, ask for changes (type them, `enter` sends, `esc` goes back), or end the task (see [Setting up verification](#setting-up-verification)) |
| `ctrl+c` | Quit |

In a picker, `j`/`k` (or the arrow keys) move and `enter` chooses. In the list of sessions, `r`
renames the highlighted session and `x` deletes it (see [Sessions](#sessions)).

### Sessions

A session is what orbit is working on: its task list, its review batches and rounds (see
[Reviews](#reviews)), the planner's conversation, the planner's model and new tasks' tool access,
and its name and when it was created and last active. The count of commits towards the next super
review isn't a session's: it belongs to the repository, kept in the store beside its sessions, and
carries on across sessions and restarts.

Orbit saves the open session after every change to its store, a SQLite database in the state
directory (see [Where orbit keeps things](#where-orbit-keeps-things)), where each repository has
its own sessions. Each save is one transaction, so a crash or `ctrl+c` leaves the last save whole.
A new session is saved once you add a task or prompt the planner; quitting before that leaves
nothing behind. Sessions are kept until you delete them: orbit never prunes them, and never
overwrites a saved session it can't read. The design, and what is deliberately left for later, is in
[docs/sessions.md](docs/sessions.md).

Every event of a session's conversation is kept in the store too, as its event log. The
conversation shows where the session left off: at startup, its last 500 events, your prompts,
the planner's turns, the agents' runs and your decisions on proposed checks under their tabs as
they were shown live, and the conversation carries on from there. Moving up past its first entry, with the conversation activated, pages
back through earlier events to the session's start, or, once its log was trimmed (see
`session_log_mb` under [Where orbit keeps things](#where-orbit-keeps-things)), to the line
"Older events were removed (session_log_mb)".

Every chat, in process or connected to an `orbit serve` (see [Serving the control plane](#serving-the-control-plane)),
starts with the session picker. Connected, `Resume <name>` comes first and is highlighted: the
session the control plane has active, which runs its work, and `enter` on it just keeps it. A chat
running its own control plane has no session to resume, so it has no `Resume`. The other saved
sessions follow, the most recently active first, then `New session`; with none saved, the picker
offers only `New session`. Each row shows the session's name, when it was last active (local time),
`active` on the workspace's active session, `running` while it runs work (a task run, a review or a
planner turn), `viewed by N` when N other frontends view it, how many of its
tasks stand at each status, by the task list's icons (`3 ✅ 2 ⏳ 1 📝`), and what they cost
(`1.2M tok · $0.84`); `●` marks the session this chat views. The marks follow the sessions live while
the picker is open, as do sessions another frontend starts, renames or deletes. `j`/`k` (or the arrow keys) move and `enter` picks the highlighted session. On
`New session`, `enter` asks for a name: `enter` starts the session, named for the date and time if
you leave the name empty, and `esc` goes back to the list. `esc` on the list quits orbit. Any draft
orbit added before you picked, such as a fix for a step that failed the startup check, carries over
into the session you pick. The startup check doesn't keep you from switching: it runs on the base
commit rather than in a session, and if it fails after you picked, its fix draft goes to the session
active then. `--new` skips the picker and starts a new session, and `--name NAME`
starts one named `NAME`.

`ctrl+p` opens the same picker later, the session this chat views first; there `esc` closes it and
keeps that session, as does picking it. Picking another session switches what runs only when the
active session is idle and no other frontend views it: the session you pick becomes the active one,
and its conversation shows in place of the one you left. Otherwise, while the active session runs
work or another frontend views it, picking a session shows it read-only, and what runs stays as it
was. Switching is refused, with an error saying what to stop, while tasks run, a review runs or the
planner answers. Any other frontend that was viewing the session you
left keeps it, read-only.

A read-only view shows the session as it was last saved, with its conversation's last events. You
can browse it, rename or delete it from the picker and page back through its history, but anything
that would change it or run work, such as a prompt, `ctrl+r` or editing a task, is refused with an
error naming the active session and how to switch. While you view one, the line above the prompt
says what the active session is doing: `viewing <name>; <active> is running <what> (enter to switch
when it's idle)` while it runs work, where `<what>` names it by the task list's numbers (`task 3`,
`the review of task 5`, `the planner`, joined when several, and `a task` for one the list lacks),
and `viewing <name>; <active> is idle (enter to switch)` otherwise. The startup check doesn't count
as running: it never keeps you from switching. This is not the prompt title's
`read-only` mark, which means another orbit owns the repository (see [Owning the
repository](#owning-the-repository)): each has its own fix, and both can show at once. Picking a
session that opens read-only asks, in a modal, whether to "Make this the active session" or "Just
look"; `enter` on the prompt, while it's selected, asks the same. Making it the active one switches
as picking does, and is refused, with an error, if work started meanwhile; "Just look" or `esc`
keeps the view. While the active session runs work, the modal says `<name> is running <what>;
you can switch once it's idle.` and offers only "Just look". If you picked the session and were held
back so, the modal comes back once when the active session turns idle, saying `<name> is idle now.`,
with "Make this the active session" highlighted and "Just look"; a frontend that only views the
session without having picked it just sees the line above the prompt change. If the session you view becomes the active one
meanwhile, say from another frontend, the modal closes.

Opening a session never starts work by
itself: its pending tasks wait for `ctrl+r`. A task that was running or being verified when the
session was last saved (orbit crashed, say) comes back cancelled, and orbit says how many were
interrupted. An interrupted review ends as a cancelled one does: if its batch is then due a
review, one is queued for the next run. The planner carries on its saved conversation, and the session's saved model and tool
access come back, unless `--model` or `--tools` was given, which hold for this run.

Each session is locked while an orbit has it open: in another orbit's list it shows `open in
another orbit`, dimmed, and can't be opened. The lock is an OS lock on a `.lock` file in the state
directory's `locks/` folder, held while the orbit runs; when its process ends (orbit crashed, say) the lock goes with
it, and the next orbit to open the session takes it. Two orbits share these locks when they use the
same state directory. A saved session that can't be read shows `unreadable:` and the reason, and
can't be opened either.

In the list, `r` renames the highlighted session, the open one included: it asks for the new name,
starting from the old one; `enter` saves it (an empty name keeps the old one) and `esc` goes back.
Names needn't be unique. `x` deletes the highlighted session after a `y`/`n` confirm (`esc` is no):
it leaves the store, with any stale lock, while Claude's own conversation files stay. The open session
can't be deleted, and a session open in another orbit can be neither renamed nor deleted; an
unreadable one can be deleted but not renamed. The list refreshes after either. On `New session`,
`r` and `x` do nothing.

The Conversation window's title ends with the open session's name, cut to 30 characters
(`Conversation — auth rework`). It follows a rename of the open session and a switch to another.

### Tasks

The task list queues prompts to run later. Each task has its own prompt, model and tool access, set
in the task editor's four fields: **Title**, the prompt's first line, which is what the task list
shows; **Prompt**, the rest of it, in a box that scrolls; **Model** and **Tools**. `j`/`k` pick a
field, `J`/`K` scroll the prompt box one line at a time (whichever field is selected), `i` types in
the title or prompt (`esc` stops; `enter` stops in the title and adds a line in the prompt),
`enter` on model or tools opens a picker, `ctrl+s` saves (from anywhere, even while typing) and
`esc` discards. A task needs a title; the prompt may be empty. `enter` on a running or verifying
task, or on a review orbit queued, only shows why it can't be edited.

The task list belongs to the open session, which orbit saves as you go (see [Sessions](#sessions)).
It keeps the topmost running or verifying task in view, scrolling just enough to show it with the
row above it, and only when it would otherwise be out of view; activating the list with `enter`
starts the highlight on that task. Activating the list, or scrolling it with the mouse wheel,
holds it: it stops scrolling by itself and its title reads `Task list · held`. The first `esc`
leaves the list still held, with its highlight where it was; `esc` again, with the Task list
selected, lets go: the list scrolls to the running task at once and follows it again. Switching
sessions lets go too, leaving list mode if the list was active.
[docs/task-list.md](docs/task-list.md) records the requirements.

With the list active, `f` on a task that failed, gave up or stopped opens a modal saying why in
full, titled `Why task 3 failed` (or `stopped`, for a cancelled task): the reason on the row's
`↳` line, then the details, such as the failing step's command and output, the agent's whole
give-up reason or the error. `j`/`k` (or the arrow keys) scroll it, `y` copies it all and `esc`
closes it. It shows the task as it was when opened, even as the list changes underneath.

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
`k` on the first entry pages back through the session's history: "Loading earlier messages…" shows
at the top while orbit reads the events before it from the event log, up to 200 at a time, and they
appear above, the mark staying on its entry; keep going to page back to the session's start. When
the event log can't be read, such as while the database stays locked, orbit refuses the page with
the reason, and `k` asks again.

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
reads `Conversation · thinking hidden` while they're hidden, and `t` again shows them. The title
ends with the open session's name, cut to 30 characters
(`Conversation · coder · thinking hidden — auth rework`), and follows renames and switches. A
message that brightens at the end of its turn appears. If the marked entry is hidden, the mark moves
to the nearest entry still shown, the one before it first. Agents' label rows follow the entries shown,
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

Each row of the task list shows its status: 📝 a draft the planner proposed, ⏳ pending, ◜ running
(spinning), 🔍 verifying, ✅ done, ❗ failed verification, 🏳 the agent gave up, 🛑 cancelled, ❌
failed. A task that failed, gave up or stopped shows why on a line under it (`↳ Unit tests
failed`), cut to the list's width. Once a task has spent anything, its row ends with the tokens and dollars it
spent (`1.2M tok · $0.84`), and the task list's subtitle shows the open session's total
(`session: 1.2M tok · $0.84`): everything the session's agents have spent, the planner's turns and
tasks since removed included. It's saved with the session, so a session opened again (or listed
with `ctrl+p`) shows the same total; a new session starts with none, and shows no total. Token
counts are rounded as in a turn's rule (see above). A failed task doesn't stop the rest; `ctrl+s` cancels the running task (stopping
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
6. Unit tests with 100% coverage: `uv run pytest --cov --cov-fail-under=100`

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
the verification steps run there too. That worktree and the reviews' snapshot worktrees are all
orbit keeps under `.git/orbit`; saved sessions live in the store. Before each task the worktree catches up with the branch you
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

### Where orbit keeps things

| Home | Where | What |
|---|---|---|
| The repository | your checkout | your code, which agents change only through commits orbit lands |
| `.git/orbit` | the repository's git directory | the coder's worktree, review snapshots, what earlier runs left there and `workspace.lock`, which says which orbit owns the repository (`.orbit/workspace.lock` outside git; see [Owning the repository](#owning-the-repository)) |
| The store | the state directory | `orbit.db`, the saved sessions, their event logs and the super review count of every repository, `locks/`, and `fallback/` for events a failed write left unsaved (see [Storage failures](#storage-failures)) |
| Config files | `.orbit/config.toml` in the repository | the verification steps, setup, roles and limits, committed with your code |
| User config | `config.toml` in the config directory | your own settings for every repository, such as `[storage]`, `[server]` and `[remotes]` |
| The token | `token` in the config directory | what frontends show `orbit serve` to connect (see [Serving the control plane](#serving-the-control-plane)) |
| Run files | `run/` in the state directory | where the `orbit serve` that owns each repository listens |
| Runtime-owned | `~/.claude` | Claude Code's own: your login, read for usage limits, and the agents' conversations |

The state directory is `--state-dir`, else `$ORBIT_STATE_DIR`, else `$XDG_STATE_HOME/orbit`, else
`~/.local/state/orbit`. Orbit creates it readable by you alone, and `orbit.db` likewise. One
database serves every repository you run orbit in, each with its own sessions, found by the
repository's path. Session locks are lock files in `locks/` there, so two orbits on one repository
keep out of each other's sessions only when they share the state directory.

Older orbits saved sessions as JSON files in `.git/orbit/sessions` (`.orbit/sessions` outside a
git repository), with the super review count in `reviews.json`. The first time orbit starts on such
a repository it imports them into the store, in one go, keeping sessions it can't read as they are
and never overwriting one the store already has; then it renames the folder `sessions.imported`
(and the count `reviews.json.imported`) and says so in the conversation. If an older orbit saves
sessions there again after that, a later import picks the next free name, `sessions.imported-2`,
`sessions.imported-3` and so on (and likewise for the count), and the note names the folder it
used. Nothing is deleted or replaced. If two orbits start at once, one imports the files and the
other finds nothing left to import. If
another orbit still has one of the old sessions open, or the files can't be read or renamed,
orbit stops before the app starts, with status 2 and a message naming the path and the cause,
having imported nothing; fix that and start orbit again. Likewise a database that can't be opened,
or whose schema is newer than this orbit, stops it with a message naming the file and the versions.

The config directory is `--config-dir`, else `$ORBIT_CONFIG_DIR`, else `$XDG_CONFIG_HOME/orbit`,
else `~/.config/orbit`. Its `config.toml` is optional; its `[storage]` table sets how much of each
session the store keeps:

```toml
[storage]
tool_output_kb = 64   # the longest text field a stored event keeps whole
session_log_mb = 200  # the most of a session's event log to keep
```

Both values are the defaults, and 0 means unlimited. A stored event's text fields (tool outputs and
arguments, the planner's and agents' messages) longer than `tool_output_kb` keep their first and
last half of it, around a marker such as `… 182 KB cut by orbit's tool_output_kb …`; what the
conversation shows live is never cut, only what's kept for later. After each batch of events it
writes, orbit checks the size of each session it wrote to: a session whose stored events come to
more than `session_log_mb` has its oldest events deleted until they come to under 90% of it, so
the newest are kept and it isn't trimmed again with every event. The newest event is always kept,
even one bigger than that on its own. Other sessions are untouched.
Paging back through a trimmed session ends at the line "Older events were removed
(session_log_mb)", and a frontend reconnecting from before the removed events gets the session
afresh. An unknown key in `[storage]`,
or a value that isn't a whole number of 0 or more, stops `orbit chat` with status 2 and a message
naming the file and the key; other tables are ignored, for later orbits' settings.

`orbit.db` can be read with `sqlite3`, but its schema isn't an API, and writing to it while orbit
may use it isn't supported.

If a write to the store fails (a full disk, say), the session stops rather than carry on
unrecorded; see [Storage failures](#storage-failures).

`orbit db backup PATH` backs the database up to `PATH`, readable by you alone, with SQLite's
online backup, so it's safe while orbit runs. It takes `--state-dir` as `chat` does, refuses (status
2) when `PATH` exists unless given `--force`, and when the state directory has no `orbit.db` says
there's nothing to back up. It refuses, even with `--force`, to write over `orbit.db` itself or its
`-wal` and `-shm` files, by any path. The copy goes to a temporary file beside `PATH` that replaces
it only once whole, so a backup that fails leaves `PATH` as it was. Copying `orbit.db` yourself while orbit runs isn't safe: the copy can
miss writes still in its `-wal` file, or catch one half done. To restore, stop every orbit using the
state directory, replace `orbit.db` with the backup, and start orbit again.

### Storage failures

When a write to the store fails, whether the event log, the session record or the super review
count, orbit halts the session instead of carrying on with work it can't record: the running
agents, the planner's turn, the reviews and the startup check are cancelled, and every command
but quitting and "Try again" is refused with `storage_failed` and the same explanation. Nothing more is written
to `orbit.db` until storage is resumed.

The explanation, shown in the conversation as an error, says which write failed, the database
and the error SQLite reported, what orbit makes of it and what to do: a full disk needs free
space; a read-only database or a permission error means checking the owner and mode of the state
directory; a database locked beyond the busy timeout means another process holds it; a missing
file means the state directory was moved or removed. An error orbit doesn't recognise is said to
be one. It also gives the `seq`s of the events that weren't saved and where they went, says that
what was saved is intact up to the last saved event, and that "Try again" or restarting orbit
resumes.

The app shows the halt in a modal titled "Session stopped: storage failed" with the whole
explanation: `enter` is "Try again", which stays open until the halt lifts, then closes, or shows
the explanation again if a write still fails; `esc` closes it. While the session is halted, a line
in red above the prompt says "Session stopped: event log failed; see the transcript" (or "session
record failed" or "review count failed", whichever write failed), until the halt lifts. Closed,
the modal opens again, with the refusal's explanation, whenever a command is refused with
`storage_failed`, such as `ctrl+r`; a session opened while halted opens it too. Quitting while the
session is halted prints the explanation to the terminal (stderr) once the app has closed.

The events that weren't saved, and every event after them while the session is halted, go to a
fallback file, one JSON envelope per line, readable by you alone:
`fallback/<workspace id>-<session id>.jsonl` in the state directory, or, if that can't be
written, `.git/orbit/fallback/<session id>.jsonl` in the repository (`.orbit/fallback/` outside
git). The explanation names the file, or says that neither place could be written.

Once the cause is fixed, "Try again" retries the writes that failed: the session record as it is
now, a saved session's rename whose save failed (dropped, with an error, if that session was
deleted or another orbit has it open since), the super review count and the event log. When they work, the events in the fallback file
and those still waiting are appended to the log in `seq` order in one go, skipping any it already
holds, the fallback file is deleted and the halt is lifted: commands are accepted again and work
can carry on. Events that reached neither the log nor a fallback file are recorded as a gap, which
the conversation, and paging back through it, shows in their place as a line such as "Events
1204–1210 weren't saved". If a write still fails, the session stays halted and the explanation
is given again for the failure now.

Restarting orbit recovers the events too. On every start, once the database is open, orbit
imports each of the repository's fallback files, from both places, into the log the same way,
skipping events already stored, deletes it, and says in the conversation how many events it
recovered from which file, as in "Recovered 37 events from .git/orbit/fallback/<session
id>.jsonl into the session's log.", counting only the events it newly stored; a file whose events
were all stored already is deleted without a word. A fallback file that can't be read or imported stops orbit
before the app starts, with status 2 and a message naming the file and the error, leaving the
file as it was.

### Planning with the planner

The prompt talks to the planner (its title reads `Prompt · planner`). It wraps what you type and
grows with it up to 8 lines, then scrolls, and shrinks again once sent; `enter` sends, and pasted
text keeps its newlines. Orbit is for planning, tasks and reviews, with no free-form chat besides.
The **planner** tab, always second in the strip, shows just the planner conversation: your
prompts, and the planner's messages, tool calls and exchanges with orbit (`ctrl+t` jumps there,
and pressed again goes back to the tab you were on). Tell it what you want to build; it asks
what's unclear in rounds of a few questions, each offering options with a recommendation, and
breaks the goal into small tasks, proposing each with `propose_task` (a title is required, and
kept to one line). Every plan's first task writes the plan's record: a numbered requirements
document in `docs/` (or wherever the project keeps such records), named like
`docs/00001-feature-dark-mode.md`, with the decisions you confirmed and the tasks that carry them
out. Every other task's prompt opens with `Plan: <path>` pointing at it, so coders and reviewers
read the same record. Its first prompt, and the first after the verification steps change, tells it
the steps each task must pass. Proposals appear in the task list as 📝 drafts, with the `coder`
role's default settings, and never run on their own: on the task list, `a` accepts the highlighted
draft (or edit it with `enter` and save, which accepts it too), and `x` removes it. Accepted tasks
run with the rest when you press `ctrl+r`.

The planner answers in a lane of its own, so you can plan while tasks run (and press `ctrl+r`
while it answers). While the planner answers, its tab shows `●` and the prompt's title reads
`Prompt · planner · busy`; you can still type in the prompt, but `enter` only says `Planner is
busy` and keeps your text, to send with `enter` once the planner is free. `ctrl+x` stops only the
planner's turn and leaves the task run going, as does `[roles.planner] timeout_minutes`, if set,
once a turn runs that long (the turn ends in error); `ctrl+s` stops the task run, whichever tab is shown. The
task list beside it fills with
drafts as the planner proposes them; when it says something while you're on another tab, the
planner tab gets a `*`.

The planner is one agent for the whole session: each prompt resumes its conversation, so it
remembers what you discussed. It reads your checkout and can search the web but can't edit your
checkout, and uses the model you picked with `ctrl+o`, which is saved with the session; with
`default` picked, it uses `[roles.planner] model` if `.orbit/config.toml` sets one.

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
read-only snapshot of that commit. The reviewer judges the change against its task's prompt and,
when the prompt opens with `Plan: <path>`, against that plan's record, then looks for bugs, gaps
in tests and unclear code. It suggests follow-ups with `suggest_task` (a title
is required, and kept to one line): each becomes a task at the end of the queue (with the `coder` role's default settings), and once the review ends
orbit runs them, unless you pressed `ctrl+s` since your last `ctrl+r`.

Suggestions from one review form a **batch**. When every task in a batch has finished, the batch's
commits are reviewed together, which may suggest more. To keep this from going on forever, each
batch is a round further from your own work, and once the batches reach `review_rounds` (default 3)
review pauses, with a note in the conversation (given again when you open a session whose review
is paused); it resumes with the next commit from one of your tasks. A review may suggest at most `review_suggestions` tasks (default 5), and a suggestion that
an unfinished task already covers is refused. With the defaults, one of your commits can lead to at
most 15 suggested tasks. Removing a suggested task drops it from its batch, which is then reviewed
without it (or closed, if nothing in it landed); removing a queued review closes the batch it would
have suggested into.

Every `super_review_every` commits (default 10, counting every commit orbit lands), a
`super-reviewer` (Opus by default) reviews the **whole codebase** at the latest commit, looking for
duplication, structure that no longer fits and gaps in tests, and may suggest up to
`super_review_suggestions` tasks (default 10). Those form a batch at round 1 and carry on like any
other review's. Each review ends with a one-line verdict that also counts the findings its cap
left out, so `5 suggestions, 2 more left out` tells you a second pass would find more. `ctrl+g` asks for one straight away and restarts the count. The count belongs to
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
working when its role's time runs out is stopped and the task fails. The planner's limit applies to
each of its turns: one that runs out of time is stopped and ends in error, and the next prompt
carries on the conversation.

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

A model picked with `ctrl+o` applies from the planner's next prompt and keeps its conversation
(with `default` picked, the planner uses `[roles.planner] model` if the config sets one); the
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

### Serving the control plane

`orbit serve` runs orbit's backend, the control plane, on its own: it serves the protocol (see
[docs/protocol.md](docs/protocol.md#transport)) over a WebSocket, and the work goes on with no
frontend connected (to host it in a container, see
[Hosting in a container](#hosting-in-a-container)). It takes `-C` and the backend flags `chat` has (`--model`, `--tools`,
`--verify`, `--worktree`, `--review`, `--usage`, `--new`, `--name`, `--profile`, which writes
`serve.prof` and `serve-blocked.log`), `--state-dir` and `--config-dir`, and:

- `--host`: the address to listen on, by default `127.0.0.1`. Without TLS only loopback is
  allowed (`127.0.0.1`, `::1`, `localhost`); another host stops it with status 2, pointing at
  `--tls`. With TLS any address is.
- `--port`: the port, by default 7433; 0 lets the system choose one. A port in use stops it with
  status 2, naming the port.
- `--tls`, `--cert`, `--key` and `--client-ca`: how it serves TLS (see **TLS** below).

In a git repository, unless `--no-worktree` is given, orbit commits the coder's work, so `orbit
serve` checks as it starts that git has an identity for commits there: `user.name` and
`user.email` from the repository's, your global or the system git config, or the
`GIT_AUTHOR_*` and `GIT_COMMITTER_*` environment variables. Without one it stops with status 2,
saying to set them (`git config --global user.name "Your Name"` and `git config --global
user.email you@example.com`) or, in a container, to mount your `~/.gitconfig` into it.

It opens the repository's most recently active saved session that no other orbit has open, or a
new one when there's none; `--new` or `--name` start a new one. Once listening it prints one line
with the repository, its URL, whether it owns the repository or is read-only (naming the owner)
and the token file (under mTLS, the client CA), then one line as each frontend connects (with the
name its `Hello` gives and its address, and under mTLS its certificate's subject and SHA-256
fingerprint) and disconnects, and one as it stops; a connection closed without a `Hello`, like the
probe `orbit chat` makes to find a server, gets no line. Warnings and errors go to stderr. While it runs, `run/<workspace id>.json` in the state directory, readable by you
alone, holds its URL and process id, and over TLS the CA a frontend on the same machine trusts and
the hostname it connects by (see **Finding the server** below); it's removed as it stops. A
read-only `orbit serve` writes it only once it takes the repository over.

The first `ctrl+c` (or SIGTERM) stops it: it stops accepting frontends (one that arrives then is
refused with HTTP 503), cancels the work in progress (stopping the agents and revoking their
tokens), writes the event log, closes every frontend's connection saying the server is shutting
down, and exits. A second `ctrl+c` exits at once.

**Health checks.** A service manager or container orchestrator can watch `orbit serve` on its
main port, without the token, with `GET /healthz` and `GET /readyz`; both answer in
`application/health+json`, and the status code is what counts:

| Path | Answer | When |
| --- | --- | --- |
| `/healthz` | 200 `{"status": "pass"}` | the server is running |
| `/readyz` | 200 `{"status": "pass"}` | it's serving, with the store open, the workspace loaded and storage working |
| `/readyz` | 200 `{"status": "warn", "output": "read_only"}` | another orbit owns the repository, so this one is read-only |
| `/readyz` | 503 `{"status": "fail", "output": "starting"}` | it isn't accepting frontends yet |
| `/readyz` | 503 `{"status": "fail", "output": "shutting_down"}` | it's stopping |
| `/readyz` | 503 `{"status": "fail", "output": "storage_halted"}` | storage is halted until resumed |

They say nothing more: no version, paths or details. Under mTLS they answer without a client
certificate too, so a probe needs no secrets.

**The token.** A frontend connects by showing the token in `token` in the config directory, sent
as `Authorization: Bearer <token>`; without it the connection is refused with HTTP 401. `orbit
serve` makes it on first start, 32 random bytes, in a file readable by you alone, and reuses it
after, making its directory readable by you alone if it has to make it. The directory must not
be writable by others, who could replace the token with one they know: a token file in a directory
others can write, or a token file others can read, stops it with status 2 and the `chmod 700` or
`chmod 600` that fixes it. Two `orbit serve`s making the token at once agree on it: the first to
write it wins, and the other uses that one. The
token grants full control of the control plane: anyone who has it can run agents in your
repository with the access you give them. It's never printed or logged; don't share it. To connect
from another machine, copy it over a trusted channel, such as `scp ~/.config/orbit/token
other:.config/orbit/token`, and reach the server over TLS (see **TLS** below), or through an SSH tunnel to
a loopback server, such as `ssh -L 7433:127.0.0.1:7433 server`. The token stays required over
TLS; under mTLS the client certificate replaces it (see **Client certificates** below).

`orbit token rotate [--config-dir PATH]` replaces the token with a new one, written whole or not at
all and readable by you alone, and says so, naming the file but never the token. A running `orbit
serve` reads the token for each frontend that connects, so the new one takes effect at once:
frontends connecting with the old token are refused with HTTP 401, while those already connected
stay connected. If a token leaks, rotate it, then restart `orbit serve` to drop every connection
made with it. An `orbit chat` reads its token file again each time it reconnects, so one connected
on the server's machine picks up the new token when it reconnects, while a frontend on another
machine needs the new copy first. Copy the new token to your other machines as before, over a
trusted channel.

The `[server]` table in your `config.toml` sets where it listens, the largest message either
way and how it serves TLS; the flags override it. The server enforces `max_message_mb` on what
it accepts and on what it sends, and chats take whatever size their server sends, so raising it
on the server is enough:

```toml
[server]
host = "127.0.0.1"   # beyond loopback only with TLS
port = 7433          # 0 lets the system choose
max_message_mb = 64  # a larger message closes the connection
tls = "off"          # "off", "tls" or "mtls" (--tls)
cert = "/home/me/.config/orbit/tls/server.pem"      # the server certificate (--cert)
key = "/home/me/.config/orbit/tls/server-key.pem"   # its private key, chmod 600 (--key)
client_ca = "/home/me/.config/orbit/tls/ca.pem"     # signs client certificates, for mtls (--client-ca)
ca = "/home/me/.config/orbit/tls/ca.pem"            # the CA that signed cert
revoked = []         # client certificate fingerprints refused under mtls
local_client_cert = "/home/me/.config/orbit/tls/local.pem"      # shown by chats here, under mtls
local_client_key = "/home/me/.config/orbit/tls/local-key.pem"   # its private key, chmod 600
```

An unknown key in `[server]`, or a value of the wrong type or out of range, stops `orbit serve`
with status 2 and a message naming the file and the key.

**TLS.** `orbit serve` serves in one of three modes, set by `tls` in `[server]` or `--tls`:

| Mode | URL | Use it when |
| --- | --- | --- |
| `off` (the default) | `ws://` | every frontend is on the same machine, or reaches it through an SSH tunnel or a reverse proxy that terminates TLS; plaintext listens on loopback alone |
| `tls` | `wss://` | frontends on other machines connect directly: traffic is encrypted, the server is verified by its certificate, and frontends show the token |
| `mtls` | `wss://` | each frontend machine should have an identity of its own that can be revoked alone: frontends show a client certificate the client CA signed rather than the token |

With `tls`, `orbit serve` needs `cert`, the server certificate, and `key`, its private key, both
PEM files; a mode missing one stops it with status 2, naming the key and its flag (`mtls` also
needs `client_ca`). Before listening it checks them, stopping with status 2 and the fix if the key
can be read by anyone but you (`chmod 600 <path>`), a file can't be read or isn't PEM, the key
isn't the certificate's, or the certificate has expired or isn't valid yet (naming its dates). A
certificate expiring within 14 days starts it with a warning. In a container, mount the
certificate and key with the config directory (see [Hosting in a container](#hosting-in-a-container)). The certificate is read once, at
start: to renew it, replace the files and restart `orbit serve`; frontends reconnect on their own.

To make a private CA and a server certificate with openssl, naming the server's hostname (here
`orbit.example.lan`, and its address) as a subject alternative name, which clients check:

```sh
mkdir -p ~/.config/orbit/tls && cd ~/.config/orbit/tls
# The CA: keep ca-key.pem secret, ideally off the server; ca.pem goes to every client.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -subj "/CN=orbit CA" -keyout ca-key.pem -out ca.pem
# The server's key and certificate, signed by the CA.
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -subj "/CN=orbit.example.lan" -keyout server-key.pem -out server.csr
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -days 365 -out server.pem \
  -extfile <(printf "subjectAltName=DNS:orbit.example.lan,IP:192.168.1.20")
chmod 600 ca-key.pem server-key.pem
orbit serve --tls tls --cert server.pem --key server-key.pem --host 0.0.0.0
```

`server.pem` and `server-key.pem` stay on the server; copy `ca.pem` and the token to each client
machine over a trusted channel, such as `scp`. Then connect by the name the certificate gives,
trusting the CA (see **Connecting** below):

```sh
orbit chat --connect wss://orbit.example.lan:7433/ --ca ~/.config/orbit/tls/ca.pem
```

**Client certificates.** With `tls = "mtls"` (`--tls mtls --client-ca <path>`), `orbit serve`
needs `client_ca` too, the CA that signs client certificates. The TLS handshake asks for a
certificate and refuses one that CA didn't sign before any WebSocket opens; a client that shows
none is let through the handshake, for the health checks, but turned away from the WebSocket,
and every other path, with HTTP 401. Either way `orbit serve` prints a line saying why. The
certificate is the client's identity: the token isn't asked for, and any sent is ignored. Each
connect line names the certificate's subject and SHA-256 fingerprint, such as `orbit-chat
connected from 192.168.1.30:51234 as CN=laptop (3F:A2:…:9C)`.

To make a client certificate for each client machine, signed by the CA above (which may serve
as `client_ca` too), make its key on the client and sign its request on the machine holding
`ca-key.pem`:

```sh
# On the client: its key, which never leaves it, and a request to sign.
mkdir -p ~/.config/orbit/tls && cd ~/.config/orbit/tls
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -subj "/CN=laptop" -keyout laptop-key.pem -out laptop.csr
chmod 600 laptop-key.pem
# On the CA's machine, given laptop.csr: the certificate, sent back to the client.
openssl x509 -req -in laptop.csr -CA ca.pem -CAkey ca-key.pem -days 365 -out laptop.pem
# Its fingerprint, as orbit serve shows it.
openssl x509 -in laptop.pem -noout -fingerprint -sha256
```

The server keeps `server.pem`, `server-key.pem` and `ca.pem` (as `client_ca`); each client keeps
its own `laptop.pem` and `laptop-key.pem` and a copy of `ca.pem` to verify the server by. Only
certificates and requests travel, never a key; `ca-key.pem` is best kept off both. Serve and
connect with:

```sh
orbit serve --tls mtls --cert server.pem --key server-key.pem --client-ca ca.pem --host 0.0.0.0
orbit chat --connect wss://orbit.example.lan:7433/ --ca ca.pem --cert laptop.pem --key laptop-key.pem
```

`orbit chat` with `--cert` and `--key` shows the certificate rather than the token, so they
don't go with `--token-file`; one without the other, a key others can read (`chmod 600`), or
files that can't be read or don't match stop it with status 2.

To revoke a lost machine's certificate, add its fingerprint, as its connect line shows it (in
either case, with or without colons), to `revoked` in `[server]`:

```toml
[server]
revoked = ["3F:A2:…:9C"]
```

`orbit serve` reads `revoked` afresh for each new connection, so there's no restart: the next
connection with that certificate is refused at the upgrade with HTTP 403, naming its fingerprint,
while one already open stays open until it disconnects (restart `orbit serve` to drop it). While
the config file is invalid, it warns and keeps the fingerprints it read last.

**TLS and connection errors.** Each message `orbit serve` stops (or warns) with, and each one
`orbit chat` stops with or shows when it can't connect, starts with what's wrong, then names the
setting, file or URL:

| Message | Fix |
| --- | --- |
| The TLS mode is missing a file. | Set the key it names in `[server]`, or pass its flag (`--cert`, `--key`, `--client-ca`). |
| Plaintext is for loopback alone. | Serve with `--tls tls`, a certificate and key, or listen on `127.0.0.1` and reach it through an SSH tunnel. |
| A TLS file can't be read. | Check the path it names, in `[server]` or the flag, and that the file is PEM (an unencrypted key). |
| The private key can be read by others. | Run the `chmod 600 <path>` it gives. |
| The certificate and key don't match. | Give the key the certificate was made for, or make a new certificate for the key. |
| The server certificate has expired. | Renew it, replace the file and restart `orbit serve`. |
| The server certificate isn't valid yet. | Check the machine's clock, or make a certificate valid from now. |
| The server certificate expires soon. | A warning: renew it and restart `orbit serve` before the date it names. |
| A client showed no certificate. | Printed by `orbit serve` under mTLS, and the body of the HTTP 401: make the client a certificate the client CA signs, and connect with `--cert` and `--key`. |
| A client's certificate isn't signed by the client CA. | Printed by `orbit serve` under mTLS: sign the client's certificate with the CA in `client_ca`, or set `client_ca` to the CA that signed it. |
| The client's certificate is revoked. | The body of the HTTP 403: give the client a new certificate, or remove its fingerprint from `revoked` to admit it again. |
| The server's certificate isn't signed by a trusted CA. | Pass `--ca` with the CA file that signed the server's certificate (the server's `ca.pem`), or set the remote's `ca`. |
| The hostname doesn't match the server's certificate. | Connect by a name the certificate gives as a subject alternative name, or reissue it with the name you connect by. |
| The server's certificate can't be used. | It fails verification for the reason given, such as having expired: renew it on the server, or check the client machine's clock. |
| The server isn't speaking TLS. | Connect with `ws://` to a server on loopback, or serve it with `--tls tls`. |
| The server is speaking TLS. | Connect with `wss://` rather than `ws://`. |
| Plaintext is refused beyond loopback. | Connect with `wss://` to a server serving TLS, or through an SSH tunnel to a loopback address. |
| The CA file can't be read. | Check the path given to `--ca`, or the remote's `ca`, and that the file is a PEM certificate. |
| The control plane refused the token. | Give it the token `orbit serve` uses: copy it to the file named, or name another with `--token-file`. |
| The server requires a client certificate. | The server serves mTLS: pass `--cert` and `--key` with a certificate its client CA signed. |
| The server rejected the client certificate. | Use a certificate signed by the CA the server has as `client_ca`. |
| The server has revoked the client certificate. | Ask the server's operator for a new certificate, or to remove its fingerprint from `revoked`. |
| The server wants a token, not a client certificate. | The server doesn't serve mTLS: pass `--token-file` (or nothing, for the default) rather than `--cert` and `--key`. |
| The client certificate can't be used. | Check the paths given to `--cert` and `--key`, and that the key is the certificate's. |
| A client certificate goes with its key. | Pass `--cert` and `--key` together. |
| Local discovery won't work without a CA. | A warning from `orbit serve` over TLS: set `ca` (or, under mTLS, `client_ca`) in `[server]`, or join with `orbit chat --connect` and `--ca`. |
| Local discovery won't work with this certificate. | A warning from `orbit serve` over TLS: add `localhost` (or the host it listens on) to the certificate as a subject alternative name, or join with `orbit chat --connect` by a name it gives. |
| Local discovery under mutual TLS needs a client certificate. | Set `local_client_cert` and `local_client_key` in `[server]` to a certificate the client CA signed and its key. |

**Connecting.** `orbit chat --connect <url>` joins a running `orbit serve`, starting with the
session picker, which offers to resume the session it has active (see [Sessions](#sessions)),
rather than running a control plane of its own. It takes a `wss://` URL, to a server serving
TLS, or a `ws://` one to loopback (`127.0.0.1`, `::1`, `localhost`); a `ws://` URL to any other
host stops it with status 2 before connecting, as it would carry the token unencrypted. Over TLS
it always verifies the server: its certificate must be signed by the CA in the file `--ca <path>`
names, or by one the system trusts without `--ca`, and must name the host connected to. There's
no way to turn either check off. A CA file that can't be read stops it with status 2. It shows
the token in `token` in the config directory, or in the file `--token-file <path>` names; a token
file that can't be read stops it with status 2. To a server serving mTLS it shows the client
certificate `--cert <path>` and `--key <path>` give instead (see **Client certificates** above). The backend flags
(`--model`, `--tools`, `--verify`, `--worktree`, `--review`, `--usage`, `--profile`) configure
the control plane, so with `--connect` they stop it with status 2: give them to `orbit serve`.
`--new` and `--name` skip the picker and open a new session once connected, as picking `New
session` does. Any
number of chats can connect at once, each showing what the others do; quitting one disconnects it
and leaves the server running.

If the connection is lost, the chat reconnects on its own, waiting half a second at first and
doubling the wait up to ten seconds, and catches up on what it missed. Meanwhile "Reconnecting to
<url>…" shows by the prompt, and a command is refused with "Not connected". After two minutes a
modal, "Lost the control plane at <url>", offers "Keep trying" or "Quit". When the server says
it's shutting down, "The control plane stopped" opens at once, with "Keep trying" preselected and
reconnecting going on behind it; either modal closes once it's connected again. A token the server
refuses shows a message naming the token file, and the chat quits once it's closed, without
retrying; so does a connection that can't be made safely as asked: a server certificate that
isn't trusted, doesn't name the host or fails verification otherwise, a server speaking TLS to
a `ws://` URL or plaintext to a `wss://` one, or under mTLS a client certificate missing,
rejected or revoked, each shown with its message from the table above. Likewise "Disconnected", giving the reason, shows when the connection closes in a way
reconnecting would only repeat: a message over the size limit, sent either way, or the server
refusing the chat's `Hello` as incompatible.

**Finding the server.** Without `--connect`, `orbit chat` looks for the `orbit serve` that owns its
repository (see [Owning the repository](#owning-the-repository)), from the run file in the state
directory, which a serve that takes the repository over writes then, and connects to it as `--connect` does, printing nothing extra: the backend flags stop
it with status 2, saying they belong on `orbit serve` or call for `--local`, and `--new` and
`--name` open a new session once connected. A run file whose process is gone, or a server that
can't be reached, is ignored, and the chat runs in-process as usual. A server found is never
bypassed: if it refuses the token, or the token file can't be read, the chat stops with status 2,
naming the token file. Only servers of the same account on the same machine are found this way; for
any other, give `--connect` its URL, or name it in `[remotes]`. `--local` skips the search and
always runs in-process, taking the backend flags; it can't go with `--connect` or `--remote`.

A server serving TLS is found too. Its run file records the CA to trust, `ca` in `[server]`, or
`client_ca` without one, and the hostname to connect by: one its certificate gives as a subject
alternative name, `localhost` first for a server listening on every address (`0.0.0.0` or `::`),
then `127.0.0.1` and `::1`; the host itself first for one on loopback, then `localhost`; and the
host itself otherwise. With no CA set, or a certificate that names none of these, `orbit serve`
starts anyway with a warning that local discovery won't work, saying why, and chats here run
in-process unless given `--connect`. Under mTLS the chat shows the client certificate
`local_client_cert` and `local_client_key` in `[server]` name, from the same `config.toml`, rather
than the token: make one with the client CA as in **Client certificates** above. Without them it
stops with status 2, naming the two keys, rather than running in-process; a key others can read
stops it with status 2 and the `chmod 600` that fixes it, as do files that can't be read.

**Named remotes.** Each `[remotes.<name>]` table in your `config.toml` names a control plane, and
`orbit chat --remote <name>` connects to it as `--connect` would with its settings:

```toml
[remotes.work]
url = "wss://orbit.example.lan:7433/"  # wss://, or ws:// to loopback
ca = "tls/work-ca.pem"                  # verifies the server (default: the system's trusted CAs)
token_file = "work-token"               # the token to show (default: token in the config directory)
# or, for a server serving mTLS, a client certificate rather than the token:
# cert = "tls/laptop.pem"
# key = "tls/laptop-key.pem"            # chmod 600
```

A relative path is taken from the config directory, and `~` is expanded. `--ca` given beside
`--remote` replaces the remote's `ca`, and `--token-file`, or `--cert` and `--key`, its token or
certificate. A remote not configured stops it with status 2, listing the names that are (or saying
none are); so do an unknown key in a remote, a `url` that isn't `wss://` or `ws://`, `cert`
without `key` or the reverse, or `cert` and `key` with `token_file`, each naming the file and the
key. `--remote` can't go with `--connect`, and the backend flags belong on `orbit serve` as they do
with `--connect`.

**Two machines.** To run the control plane on a server, `orbit.example.lan` (192.168.1.20), and
chat from a laptop: on the server, make the CA and the server's certificate as in **TLS** above,
in `~/.config/orbit/tls`, and serve with

```toml
# The server's ~/.config/orbit/config.toml
[server]
host = "0.0.0.0"
tls = "tls"   # or "mtls", with client_ca
cert = "/home/me/.config/orbit/tls/server.pem"
key = "/home/me/.config/orbit/tls/server-key.pem"
ca = "/home/me/.config/orbit/tls/ca.pem"
# client_ca = "/home/me/.config/orbit/tls/ca.pem"   # for mtls
```

and `orbit serve`, which makes the token on first start. Then copy `ca.pem` and, under `tls`, the
token to the laptop over a trusted channel, keeping both readable by you alone:

```sh
# On the laptop.
mkdir -p ~/.config/orbit/tls && chmod 700 ~/.config/orbit
scp orbit.example.lan:.config/orbit/tls/ca.pem ~/.config/orbit/tls/work-ca.pem
scp orbit.example.lan:.config/orbit/token ~/.config/orbit/work-token   # under tls
chmod 600 ~/.config/orbit/work-token
```

Under `mtls` copy no token: make the laptop's key and certificate as in **Client certificates**
above, its key staying on the laptop and only `laptop.csr` and `laptop.pem` travelling. On the
laptop, name the server:

```toml
# The laptop's ~/.config/orbit/config.toml
[remotes.work]
url = "wss://orbit.example.lan:7433/"
ca = "tls/work-ca.pem"
token_file = "work-token"    # under tls
# cert = "tls/laptop.pem"    # under mtls, instead of token_file
# key = "tls/laptop-key.pem"
```

and connect with `orbit chat --remote work`. In all, the server keeps `ca.pem`, `server.pem`,
`server-key.pem` and the token (`ca-key.pem` is best kept off it); the laptop keeps its copy of
`ca.pem`, and the token's copy under `tls`, or `laptop.pem` and `laptop-key.pem` under `mtls`.

### Hosting in a container

The recommended way to host the control plane is in a container, built from this repository's
own files: `deploy/Dockerfile` makes an image of `orbit serve` (Python 3.13, git, Node.js LTS and
the Claude Code CLI pinned by the `CLAUDE_CODE_VERSION` build argument, and orbit installed from
the checkout with `uv sync --frozen --no-dev`), and `deploy/compose.yaml` runs it over TLS. The
image holds no credential: Claude's login, the token, the certificates and their keys live in
volumes, and `.dockerignore` keeps `.env`, `orbit-config`, keys and `.git/orbit` out of the build.

The compose file runs one service, `orbit`, as your user (`user: "${UID:-1000}:${GID:-1000}"`,
so the commits it makes in your checkout are yours), with these mounts:

| Mount | In the container | What |
| --- | --- | --- |
| the repository (`..`) | `/workspace` | the checkout `orbit serve -C /workspace` works on; git trusts it through `safe.directory` |
| `deploy/orbit-config` | `/home/orbit/.config/orbit` (`ORBIT_CONFIG_DIR`) | `config.toml`, the token and the certificates |
| the volume `orbit-state` | `/home/orbit/.local/state/orbit` (`ORBIT_STATE_DIR`) | the store and run files |
| the volume `claude-login` | `/home/orbit/.claude` (`CLAUDE_CONFIG_DIR`) | Claude Code's login |
| `~/.gitconfig`, read-only | `/home/orbit/.gitconfig` | your commit identity |

Bash doesn't export `UID` or `GID`, so unless your ids are 1000, run `export UID; export GID=$(id
-g)` first, or put both in `deploy/.env`; the image is built with the same ids.

To set it up, from the repository's top level:

1. Make the CA and the server's certificate as in **TLS** above, naming the host clients reach
   the server by, and put `server.pem` and `server-key.pem` in `deploy/orbit-config/tls`:

   ```sh
   mkdir -p deploy/orbit-config/tls && chmod 700 deploy/orbit-config
   cp server.pem server-key.pem deploy/orbit-config/tls/ && chmod 600 deploy/orbit-config/tls/server-key.pem
   ```

2. Write `deploy/orbit-config/config.toml`, serving TLS on every address inside the container with
   the paths as the container sees them:

   ```toml
   [server]
   host = "0.0.0.0"
   tls = "tls"   # or "mtls", with client_ca
   cert = "/home/orbit/.config/orbit/tls/server.pem"
   key = "/home/orbit/.config/orbit/tls/server-key.pem"
   ```

3. Log Claude in once, into the `claude-login` volume, or put `ANTHROPIC_API_KEY=...` in
   `deploy/.env` (kept out of git and the image) instead:

   ```sh
   cd deploy
   docker compose build
   docker compose run --rm --entrypoint claude orbit login
   ```

4. Start it: `docker compose up -d`. `orbit serve` takes the commit identity from your
   `~/.gitconfig`, and stops with status 2 if it has none (see **Serving the control plane**
   above). It makes the token in `deploy/orbit-config/token` on first start; `docker compose
   logs orbit` shows its line with the URL.

To connect from another machine, copy `ca.pem` and the token (under `mtls`, make the client its
certificate instead) as in **Two machines** above, name the server in `[remotes]`, and run
`orbit chat --remote work`.

The healthcheck asks `/readyz` with `curl` inside the container every 30 seconds, so `docker
compose ps` shows the container healthy once `orbit serve` answers 200: `pass` when it's serving,
or `warn` (`read_only`) when another orbit owns the repository. A 503 (`starting`,
`shutting_down` or `storage_halted`, see **Health checks** above) marks it unhealthy; storage
halted needs your attention, not a restart.

To back up the store, back it up inside the container and copy the backup out:

```sh
docker compose exec orbit orbit db backup /home/orbit/.local/state/orbit/backup.db --force
docker compose cp orbit:/home/orbit/.local/state/orbit/backup.db ./orbit-backup.db
```

On a single Linux machine you can skip TLS: the compose file's commented variant uses the host's
network (`network_mode: host`, no `ports:`), with `host = "127.0.0.1"` and `tls = "off"` in
`config.toml`, and a healthcheck over `http://`. Frontends on the same machine then connect to
`ws://127.0.0.1:7433/` with the token in `deploy/orbit-config/token`.

A manual smoke test, after the setup above:

```sh
cd deploy
docker compose up -d --build
docker compose ps                     # healthy, once it's serving
curl -fsk https://127.0.0.1:7433/readyz   # {"status": "pass"}
orbit chat --connect wss://orbit.example.lan:7433/ --ca ca.pem --token-file orbit-config/token
```

What the container doesn't protect: the agents run inside it as the same user as `orbit serve`, so
they can read and change the control plane's own files, the token, the certificates' keys, the
store and Claude's login, like the rest of the container; the container keeps them from your host,
not from orbit's agents. And one container serves one repository: run workspaces that trust each
other differently in containers of their own, each with its own config, token and volumes.

### Owning the repository

The first orbit to reach a repository, an `orbit serve` or an `orbit chat` running in-process,
owns it and runs its work; two orbits never both run work on one repository. It holds an
exclusive lock on `.git/orbit/workspace.lock` (`.orbit/workspace.lock` outside git), which names
its process id and, for `orbit serve`, its URL. The system lets go of the lock when the owner's
process ends, even in a crash, so the next orbit to start owns the repository. A lock that can't
be made stops orbit with status 2, naming the file. A chat connected to an `orbit serve` takes no
lock of its own: the server does.

Any other orbit started on the repository meanwhile is read-only. It opens with a note naming the
owner, such as "Read-only: an orbit chat, pid 4242 owns this repository. Tasks you run are queued
until this orbit owns it.", and the prompt's title carries a "read-only" mark. `orbit serve`'s
first line says so too, and it writes no run file, leaving the owner's for chats to find. Running
tasks (`ctrl+r`) and the triggers (`ctrl+g`, `ctrl+e`) are accepted and queued, in order, but
nothing runs: no task, review or startup check. Those an owning orbit would refuse, such as a super
review with reviews off, are refused just the same. It notes no work left in `.git/orbit` by
earlier runs, since what's there may be the owner's running work. The planner, the sessions, drafts
and settings work as usual. The queue is dropped when the orbit exits.

A read-only orbit tries the lock every 5 seconds. Once the owner has gone, it takes the lock and
asks, once, in a modal: "The repository is free". `y` takes it over: the orbit owns the
repository from then on, the "read-only" mark goes, `orbit serve` writes its run file so chats
find it (a run file it can't write is reported on stderr, and it serves on), a note says "This orbit now owns the
repository; queued work is running." and the work queued runs, in order. `n` or `esc` stays
read-only: the orbit lets the lock go again, so another orbit can take it, and asks again once
another orbit has owned the repository and gone, or after 5 minutes if it's still free. Under `orbit serve`, every frontend connected asks, as does
one that connects while the question is open; once one answers, the others close the modal. An
orbit that owns the repository never asks.

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
  interface, the `Link` a session to a control plane elsewhere reports its state through (in
  `link`, with `NotConnected`), the WebSocket transport's close codes, 401 status, default
  message limit and `bearer_header` (in `transport`), which the control plane and `orbit.client`
  share, and `ThreadedSession`, which runs a session on a thread of its own. It imports no
  other orbit package. `docs/protocol.md` describes the protocol, its conversation rules and
  codes, and `schema/protocol.schema.json` is its JSON Schema.
- `orbit.client`: the frontends' side of a control plane in another process. `WebSocketSession`
  (in `session`) is a `Session` over a WebSocket (the `websockets` library) that reconnects with
  backoff, but not after a close that would recur (1000 or 1009), sends its last `Hello` again
  with `since`, and reports how the link stands through
  `orbit.protocol`'s `Link`; `read_token` (in `token`) reads the token it shows, and `accepts`
  (in `probe`) asks a control plane whether it takes it; both open the WebSocket through
  `open_connection` (in `upgrade`), which raises `TokenRejected` on a 401. It imports no
  orbit package but `orbit.protocol`, and only `orbit.cli` imports it.
- `orbit.agent`: backend, with no UI imports. Three layers, each importing only the ones below:
  - `external`: adapters that reach outside the backend. `ClaudeCliRunner` launches agents as
    `claude -p --output-format stream-json` runs connected to orbit (`run_agent`, and `converse`
    for the planner, which resumes its Claude session turn after turn); `McpHost` is orbit's
    MCP server; `GitCoderTree` is the coder's worktree and its snapshots; `repository` finds the
    git repository a workspace is in, fingerprints it, and places orbit's directory in it
    (`orbit_dir`: `.git/orbit`, or `.orbit` outside git); `config_file` reads
    `.orbit/config.toml`; `built_in_roles` reads the built-in roles' instructions from
    `instructions/*.md`; `checks` runs a verification step as a process (`run_step`);
    `processes` runs a program to completion without leaving it behind; `workspace`
    fingerprints the git working tree; `usage_api` fetches the Claude subscription's usage
    limits (`UsageApi`) with the token in `~/.claude/.credentials.json`; `sqlite_store` is the
    `SessionStore` port's adapter (`SqliteStore`): saved sessions and each repository's super
    review count in `orbit.db` in the state directory, with `session_locks`' lock files while
    an orbit has a session open, `sqlite_schema` creating, migrating and opening the database,
    `sqlite_backup` copying it to a backup and `sqlite_errors` telling its errors apart;
    `directories` picks the state and config directories (`resolve_state_dir`,
    `resolve_config_dir`); `session_import` imports the session files
    older orbits saved in orbit's directory, under the names `session_files` gives. `WorkspaceLock`
    (in `workspace_lock`) is the lock the orbit owning a repository holds.
    `ControlPlaneServer` (in `control_plane`) is the server adapter: it serves `AgentSession`
    over a WebSocket (starlette on uvicorn, through `embedded_server`'s `EmbeddedServer`, which
    leaves signals to the program), one `SessionConnection` per frontend that shows the token,
    turning frames into commands and events into frames; `TokenFile` (in `token_file`) is the
    token it checks, read afresh for each frontend, and rotates it, `tls_warnings` (in `tls`) checks
    the server's TLS settings, key and certificate before it listens, with the messages in
    `tls_messages`, `RunFile` (in `run_file`) says where it listens, both written through
    `private_files`, and `user_config` reads `[storage]`, `[server]` and `[remotes]`.
  - `application`: `AgentSession` (in `session`) implements `Session`: it takes commands, asks
    the domain what follows, drives the ports and publishes events into the `Outbox` (in
    `outbox`), which stamps each with its `seq` and the session it belongs to. `Tasks` (in
    `tasks`) holds the `TaskBoard`, marks tasks running and finished, and is the one place that
    publishes the board, whole, once its callers have changed it. `WorkLane` (in `work_lane`) is
    the work lane: it runs the board's tasks one at a time, queues a fix after each failed step
    and lands work that passes, and `WorkTurns` (in `work_turns`) decides when it runs: when the
    user asks (`ctrl+r`), when a review queues work unless the user held it, and first for a
    configurator. `AgentRun` (in `agent_run`) sees one task through one agent, and a
    `ProposalTrial` (in `proposal_trial`) tries the checks a configurator proposes and has the
    user decide on them; `AgentDesk` (in `desk`) launches agents and routes their calls by token;
    `Verifier` (in `verifier`) runs the steps; `Lander` (in `landing`) commits and merges
    verified work; `ReviewLane` (in `review_lane`) runs reviews beside the work; `StartupCheck`
    (in `startup_check`) runs the steps once on the base commit as orbit starts; `UsageMonitor`
    (in `usage_monitor`) fetches the usage limits on a schedule and after an agent's turn; these
    three run their work through `Background` (in `lanes`), which reports a crash rather than
    losing it, and the work lane's and the planner's turns run through a `TurnSlot` each (also in
    `lanes`), which refuses a second turn and shows the lane busy while one runs;
    `FrontendReview` (in `proposals`) is where the user decides on proposed checks; `PlannerChat`
    (in `planner`) is the planner's conversation, running each turn and publishing how it ended;
    `PlannerSeat` (in `planner_seat`) is the planner's place in the session: it holds the current
    `PlannerChat` and its turns, starts, cancels and resets them, and closes the planners a
    switch or reset retired once that's done; `SessionKeeper` (in `keeper`) is the open session:
    it saves it after every change once it's used, and opens, renames and deletes saved sessions
    through the store, each under its lock; `SessionSwitcher` (in `switcher`) switches the
    session between saved ones: it lists, opens, renames and deletes them through the keeper,
    opens another or a new one in place of the one open, and holds what the session's agents have
    spent; `WorkCommands` (in `work_commands`) handles the other commands, the prompts, tasks,
    board and agents' work, refusing those that name no such agent, lane or trigger, and
    queueing those that start work while the session is read-only; `Takeover` (in `takeover`)
    keeps whether the session is read-only, which the other parts read through it, tries a
    read-only session's workspace lock, through the `RepositoryLock` port, until the
    repository is free, and holds the offer of it until the user answers;
    `ReviewCount` (in `review_count`) keeps the repository's super review count apart from
    any one session; `SessionSettings` (in `settings`) holds the open session's settings, at
    first those given at startup (a session opened brings its saved ones, but for the pinned
    ones, which keep the given), and the models they may choose from; `outcome` and
    `ReportedOnce` (in `failures`) turn a failing call to the store into its error and report a
    failing save once, until a save works again; `Handshake` (in `handshake`) opens every stream:
    it refuses commands until `Hello`, answers it with `Welcome` and publishes what the frontend
    needs to show the session, then starts the background work. `ports` defines what it needs
    from outside (`AgentHost`, `AgentLauncher`, `CoderTree`, `StepRunner`, `Workspace`,
    `ProposalReview`, `UsageSource`, `ConfigWriter`, `SessionStore`).
  - `domain`: the rules, with no I/O: the `TaskBoard` (in `board`: task order, statuses, fix
    chains) and what a crash leaves of its tasks (`interrupted`), `endings` (why a task ended
    other than completed: a line for the board and the full text), `refusals` (`Refused`, a
    command the rules won't carry out, and why), `verification` (`StepProgress`, each
    verification step's status, kept by the `Verifier` and the startup check; when a task needs
    verifying, how many fixes to try per step, and the fix prompt), `AgentTask` (in `agent_task`:
    what orbit replies to an agent, and when its work is verified), the `AgentRegistry` (in
    `agents`: agent ids and tokens), `roles` (built-in roles, given their instructions, and the
    tasks orbit queues in them), `config` (`OrbitConfig`: the limits, roles, verification steps
    and setup a project file sets over the built-in defaults) and `config_errors` (`ConfigError`,
    and what's wrong with an invalid file or proposal), `server` (`ServerSettings`, the user config's `[server]` with its `TlsSettings`, which files each TLS mode needs, where now falls in a certificate's validity, and which hosts are loopback), `remotes` (`RemoteSettings`, a `[remotes.<name>]` table, and which credentials go together), `ownership` (`WorkspaceOwner`, who a workspace lock names, and the read-only note), `checks` (the verification steps, the
    setup step that makes a fresh worktree ready, and the checks a configurator proposes), the
    `Catalog` (in `catalog`: the workspace and its sessions, the agents' roles, the triggers and
    the lanes), `ReviewPlanner` (in `reviews`: when to review, batches, rounds and caps), commit
    messages (`commits`) and `usage` (parsing the subscription's usage limits, and when to fetch
    them again). `sessions` holds the rules of saved sessions: the `SessionRecord` and its format
    (versioned, with upgrades from older ones, or why a file is unreadable), how the list of
    sessions summarizes and orders them, when one may be opened, renamed or deleted, and the
    notes and settings a resumed session starts with.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`.
  `view` turns state and events into text and doesn't import Textual, with a module per area of
  the screen (`events`, `transcript`, `tool_calls`, `proposals`, `tasks`, `panels`, `agents`,
  `tabs`, `detail` and `detail_views` for the detail pane, `diffs` and `diff_numbers` for an
  edit's diff, `progress` for notes on a task's run, `rules` for the rules ending turns and
  agents' runs, `turns` for the conversation's running turns, `working` for the working row,
  `log` for which of a tab's transcript entries are shown and where, `sessions` for the list of
  sessions, `windows` for the window h/j/k/l reaches from each window, `triggers` for the keys
  that fire triggers and their footer labels, `confirm` for the confirmations asked before a
  key's action, `link` for how the link to a control plane elsewhere is shown, and `settings` and
  `common` for what they share); the widgets (`transcript`,
  `transcript_log` for one tab's log, `event_stream` for batching the backend's events so a
  flood still lets the screen repaint, `conversation` for the `ConversationFeed`, which turns
  events into the transcript's entries, `entries` for an entry and the renderables that draw
  it, `tabs`, `tasks`, `task_editor` for the task editor modal, `reason` for the modal saying
  why a task ended, `link` for the status line and modals showing a lost or stopped control
  plane, `agents`, `panels`,
  `detail` for the detail pane, `working` for the working row, `proposals`, the proposal modal
  and the `ProposalHost` that shows each waiting proposal once and closes it when it's withdrawn,
  `sessions`, the modal listing the sessions and the `SessionsHost` that shows it and refreshes
  it while it's open, `list_pane` for the list pane the lists share, `footer` for the key footer,
  and `widgets` for the building blocks they share) lay it out, and `app` is the Textual app that
  holds them.
- `orbit.cli`: the composition root, and the only place the two sides are wired together: it
  plugs the external adapters into `AgentSession` and hands that to the TUI. It runs the
  backend on its own thread and event loop through `orbit.protocol.ThreadedSession`, so neither
  side's work stalls the other. `orbit serve` plugs them in the same way but hands `AgentSession` to
  `ControlPlaneServer` instead, on the main thread's event loop. `orbit chat --connect` builds no
  backend: it hands the TUI an `orbit.client.WebSocketSession`, which runs on the TUI's own event
  loop; neither does a chat that `discover_control_plane` finds a running `orbit serve` for.
  It's a module per command (`chat`, `serve`, and `maintenance` for `version`, `db` and
  `token`), with `backend` wiring the adapters for both chat and serve, `options` the options
  they share and `errors` how they exit; its `__init__` registers the commands on the app.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
