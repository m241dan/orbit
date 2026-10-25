# orbit

Orbit command-line tool.

## Install

Requires [uv](https://docs.astral.sh/uv/) and the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`) on your `PATH`. Orbit runs on Python 3.14, which uv fetches if it isn't installed.

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
no setup runs. `orbit config show` prints every setting in effect and where it came from (see
[Seeing the configuration in effect](#seeing-the-configuration-in-effect)).

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
uv run orbit db forget ~/code/old              # delete everything orbit keeps of a workspace
uv run orbit serve --add . --add ~/code/other  # run the control plane on its own, serving both
uv run orbit serve                             # serve the projects added before again
uv run orbit serve --port 0                    # on a port the system chooses
uv run orbit serve --tls tls --cert server.pem --key server-key.pem --host 0.0.0.0  # over TLS
uv run orbit chat --connect ws://127.0.0.1:7433/  # join the orbit serve running there
uv run orbit chat --connect wss://orbit.example.lan:7433/ --ca ca.pem  # over TLS
uv run orbit chat --remote work                # connect to the control plane [remotes.work] names
uv run orbit chat --local                      # run in-process even with orbit serve running
uv run orbit status                            # what the orbit serve running here is doing
uv run orbit status --remote work --json       # the same of another, as one JSON object
uv run orbit config show                       # every setting in effect here, and where it's from
uv run orbit config show roles.reviewer --full # one role's, with its instructions whole
uv run orbit token rotate                      # replace the token frontends show orbit serve
uv run orbit certs init orbit.example.lan      # make a CA and a server certificate for TLS
uv run orbit certs server orbit.example.lan    # renew the server certificate
uv run orbit certs client laptop               # issue a client certificate for mutual TLS
uv run orbit certs client desktop --bundle desktop.tar  # the same, bundled to copy to the desktop
uv run orbit certs list                        # each certificate's expiry and fingerprint
uv run orbit version                           # show the installed version
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
| `i` | Jump to the prompt, which talks to the conversational role whose tab is shown, or the planner on any other tab, and start typing (vim-style insert mode): long text wraps and the box grows up to 8 lines, then scrolls; `enter` sends, pasted text keeps its newlines, `esc` stops typing |
| `enter` | On the conversation: activate it (`j`/`k` move between entries, `k` on the first loading earlier ones, `enter` collapses or expands the one marked `▌` in the left gutter, or opens the detail pane on a tool call or request, `J`/`K` scroll the open detail pane, `y` copies its call's whole output, `tab` activates it, `esc` closes the detail pane if it's open, else leaves and holds the marked entry); again, resume at the held entry |
| `enter` | On the conversation's detail pane: activate it (`j`/`k` scroll it, `y` copies its call's whole output, `esc` leaves with the pane still open) |
| `enter` | On the task list: activate and hold it (`j`/`k` move, `enter` edits a task or adds one on `+ new task`, `a` accepts a draft, `x` removes a task, `f` shows why a task failed or stopped, `L` narrows it to each lane its tasks run in, in turn, then shows them all again, `esc` leaves, the list still held) |
| `enter` | On the agents pane: activate it (`j`/`k` move, `c` cancels the highlighted agent's work, `esc` leaves) |
| `enter` | On the prompt, while viewing a session read-only: ask whether to make it the active session (see [Sessions](#sessions)) |
| `t` | Hide agents' dim messages, still thinking out loud, in every tab; again, show them (works with the conversation activated too) |
| `ctrl+b` | Hide the sidebar so the conversation takes the full width (`h`/`j`/`k`/`l` skip its windows meanwhile); again, show it as it was (not while typing) |
| `ctrl+t` | Jump to the **planner** tab; again, to each other conversational role's tab in turn, and from the last, back to the tab you were on (with the built-in roles alone, the planner tab and back) |
| `ctrl+r` | Run the tasks that haven't completed yet |
| `ctrl+g` | Ask for a super review of the whole codebase now (works while tasks run; hidden while the `super-reviewer` is disabled) |
| `ctrl+e` | Set up (or redo) the verification steps: after you confirm (`y` to go ahead, `n`/`esc` to cancel), queue a configurator task first and run it (see [Setting up verification](#setting-up-verification); hidden while the `configurator` is disabled) |
| `ctrl+o` | Pick the model of the conversational role whose tab is shown, or the planner's on any other tab (default, fable, opus, sonnet, haiku; new tasks opened there default to it too) |
| `ctrl+l` | Pick the tool access level new tasks start with in the task editor (off, restricted, full), kept with the conversational role whose tab is shown, or the planner's on any other tab |
| `ctrl+y` | Pick the effort of the conversational role whose tab is shown, or the planner's on any other tab (unset, low, medium, high, xhigh, max; new tasks opened there default to it too); not while typing, where it's the prompt's redo |
| `ctrl+p` | Session picker: view or switch to a saved session, or start a new one (see [Sessions](#sessions)) |
| `ctrl+w` | Workspace picker, connected to an `orbit serve`: move to another workspace it serves, add one or remove one (see [Workspaces](#workspaces)); not while typing |
| `esc` | Stop typing, close the conversation's detail pane, leave a list or close a picker; with the conversation selected and nothing active, let go of its held entry, closing the detail pane, and follow the latest output again; with the detail pane selected, close it; with the Task list selected and nothing active, let go of the held list and follow the active task again |
| `ctrl+s` | Stop the task run (on any tab) |
| `ctrl+x` | Stop the turn of the conversational role whose tab is shown, or the planner's on any other tab, leaving the task run going |
| `ctrl+n` | Reset the conversational role whose tab is shown, or the planner on any other tab: after you confirm (`y` to go ahead, `n`/`esc` to cancel), end its conversation so the next prompt starts a fresh one, the planner's with its role read again from `.orbit/config.toml` and your `config.toml` (see [Planning with the planner](#planning-with-the-planner)) |
| `enter` / `esc` | On a storage halt: try again, or close it; `j`/`k` (or the arrow keys) scroll a long explanation (see [Storage failures](#storage-failures)) |
| `a` / `r` / `x` | On a proposal of checks: accept it, ask for changes (type them, `enter` sends, `esc` goes back), or end the task (see [Setting up verification](#setting-up-verification)) |
| `ctrl+c` | Quit |

In a picker, `j`/`k` (or the arrow keys) move and `enter` chooses. In the list of sessions, `r`
renames the highlighted session and `x` deletes it (see [Sessions](#sessions)); in the list of
workspaces, `x` removes the highlighted workspace (see [Workspaces](#workspaces)).

### Workspaces

Connected to an `orbit serve` (see [Serving the control plane](#serving-the-control-plane)), a
chat starts with the workspace picker, as one server serves many workspaces; `ctrl+w` opens it
again later. A chat running its own control plane works on its repository alone, so it has no
workspace picker and opens on that repository. The picker lists:

- `Add <path>` first, when the repository the chat runs in isn't among the workspaces served:
  `enter` adds it, then goes on as for a workspace served. Only a server on this machine is
  offered it: one the chat found running here, or one it joins by a URL whose host is loopback
  (`127.0.0.1`, `::1`, `localhost`). A path here means nothing to a server on another host, so
  joined by any other URL, with `--connect` or `--remote`, the picker neither offers it nor
  highlights the workspace at it.
- The workspaces served, each with its label, its path, its active session's name and whether
  that one is `running` or `idle`, the agents running in it by role (`2 coders · 1 reviewer`,
  nothing while none runs), where its active session waits in line for a coder (`waiting: 2nd`,
  `waiting: next`; see Taking turns in [Serving the control plane](#serving-the-control-plane)),
  and `read-only` while another orbit owns its repository. The picker watches the server's
  control-plane stream while it's open, so the agents stay current, and stops when it closes.
  The workspace of the repository the chat runs in, on a server on this machine, is
  highlighted, else the first.
- A row for each clone under way, `Cloning <name>… 42%` (or git's phase while it gives no
  percentage), until the clone is served and listed in its place. It goes if the clone fails,
  or the server refuses or fails to serve it, and every clone's row goes once a lost link to
  the server is back, as clones under way stop with the server.
- `Add a path or URL…`: `enter` opens the explorer (below).

`enter` on a workspace opens its session picker, `Resume <name>` first (see
[Sessions](#sessions)), and picking a session there moves the chat to that workspace. `x` asks
whether to stop serving the highlighted workspace: `Keep its sessions` (preselected) keeps them
for when it's added again, `Delete its sessions too` purges its records (see Purging in
[Serving the control plane](#serving-the-control-plane)) and, for a clone the server made, names its directory,
which goes too, and `Cancel` or `esc` removes nothing. It's refused, saying why under the list,
while the workspace runs work or another frontend views one of its sessions, or, when purging,
while another orbit owns its repository, naming it, or its clone holds work its remote doesn't,
listing what would be lost. `esc` on
the list quits orbit at startup, and later closes the picker. When the workspace this chat is on
is removed, the workspace picker opens, or stays open if it's removed from there, and from then
on it works as at startup: `esc` quits orbit, as there's no workspace left to go back to.

The explorer browses the directories on the server's host, as the server lists them, so a
frontend on another machine can find a repository there. It fills the screen: the path or URL
field at the top (below), the directory listed as its title, and its
directories below, repositories first, marked `⎇`, a workspace served marked `(served)` and a
symlink `→ <target>`. It starts in the directory holding the workspace added last, else the
server's `browse_root`, else its account's home. `j`/`k` move; `l` or `enter` lists the
highlighted directory, and `h` the one above; `enter` on a workspace served opens its session
picker, as picking it in the list does; `a` adds the highlighted directory, or a symlink's
target, then opens the added workspace's session picker; `.` shows or hides hidden directories (hidden at first);
`/` types in the field; `esc` goes back to the list. A listing that's refused, such as
`Permission denied: <path>`, or an add that's refused, shows why under the listing, which stays
as it was. A directory holding more than 2,000 directories lists 2,000, with a last line saying
how many more there are, to be typed in the field instead.

In the field, `enter` adds what's typed: a repository's whole URL, without spaces, as the server
takes one (`https://…`, `ssh://…`, `file://…` or `user@host:path`), is cloned, back in the list, where its `Cloning` row shows how it goes,
and if it fails, why shows in the field's hint, with the URL back in the field;
anything else is added as a path on the server's host, then opens the added workspace's session
picker, as `a` does. A refusal shows in the field's hint. `tab` completes the path's last
directory name, from the listing shown when the path is in its directory, else from a listing
of the path's directory asked for alone, which the explorer doesn't move to; hidden
directories complete only while they're shown. One match completes with a trailing `/`;
several complete as far as they agree, listed under the field. `esc` returns to the listing.

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
When the event log can't be read, such as while the database stays locked, the conversation
opens empty, followed by an error saying its events couldn't be read and why, so an empty
conversation isn't taken for the session's.

Every chat, in process or connected to an `orbit serve` (see [Serving the control plane](#serving-the-control-plane)),
starts with the session picker, connected once you've picked the workspace (see
[Workspaces](#workspaces)). Connected, `Resume <name>` comes first and is highlighted: the
session the control plane has active, which runs its work, and `enter` on it views it. A chat
running its own control plane has no session to resume, so it has no `Resume`. The other saved
sessions follow, the most recently active first, then `New session`; with none saved, the picker
offers only `New session`. Each row shows the session's name, when it was last active (local time),
`active` on the workspace's active session, `running` while it runs work (a task run, a task in a
lane of its own, a review or a planner turn), or `waiting for a coder: 2nd in line` while its task run waits for a coder (see
[Taking turns](#serving-the-control-plane); `next in line` when it's next), `run paused: usage
limit; starts again at 18:01` while its run waits to start again (see **Usage limits** under
[Tasks](#tasks)), `viewed by N` when N other frontends view it, how many of its
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
was. Switching is refused while tasks run, a task runs in a lane of its own, a review runs or the
planner answers: the picker opens
again, saying what to stop under the list, and nothing goes to the conversation; so does any other
pick that's refused or can't be sent. Any other frontend that was viewing the session you
left keeps it, read-only.

A read-only view shows the session as it was last saved, with its conversation's last events. You
can browse it, rename or delete it from the picker and page back through its history, but anything
that would change it or run work, such as a prompt, `ctrl+r` or editing a task, is refused with an
error naming the active session and how to switch. A refused prompt leaves the conversation, and
its text stays in the prompt box, which clears only once a prompt is accepted (and only if you
haven't edited it since); the same goes for a prompt sent while disconnected. While you view one, the line above the prompt
says what the active session is doing: `viewing <name>; <active> is running <what> (enter to switch
when it's idle)` while it runs work, where `<what>` names it (`a task`, `the review of a task`,
`the planner`, joined when several; tasks go unnumbered, since the task list shows the session
viewed, not the active one's), `viewing <name>; <active> is waiting for a coder: 2nd in line
(enter to switch when it's idle)` while its task run waits for a coder, after what else runs
(`is running the planner and waiting for a coder: next in line`), and `viewing <name>; <active>
is idle (enter to switch)` otherwise. The startup check doesn't count as running: it never keeps you from switching. This is not the prompt title's
`read-only` mark, which means another orbit owns the repository (see [Owning the
repository](#owning-the-repository)): each has its own fix, and both can show at once. Picking a
session that opens read-only asks, in a modal, whether to "Make this the active session" or "Just
look"; `enter` on the prompt, while it's selected, asks the same. Making it the active one switches
as picking does, and is refused, with an error, if work started meanwhile; "Just look" or `esc`
keeps the view. While the active session runs work, the modal says `<name> is running <what>;
you can switch once it's idle.` (or `<name> is waiting for a coder: …` as the line above the
prompt says it) and offers only "Just look". If you picked the session and were held
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
unreadable one can be deleted but not renamed. A refusal says why under the list, or in the name
box's hint while it's shown, until the next rename or delete; nothing goes to the conversation.
The list refreshes after either. On `New session`, `r` and `x` do nothing.

The Conversation window's title ends with the open session's name, cut to 30 characters
(`Conversation — auth rework`). It follows a rename of the open session and a switch to another.

### Tasks

The task list queues prompts to run later. Each task has its own prompt, role, model, tool access
and effort, set in the task editor's six fields: **Title**, the prompt's first line, which is what
the task list shows; **Prompt**, the rest of it, in a box that scrolls; **Role**, the role whose
agent takes the task, picked among the enabled roles that take tasks (those that aren't
conversational), each shown with its lane (`Coder · work`) and a personal role marked so
(`helper · helper · personal`; see [Your personal roles](#your-personal-roles)), and `coder` for a
new task; **Model**;
**Tool access**; and **Effort**: unset, `low`, `medium`, `high`, `xhigh` or `max`, where unset
leaves it to the role's (see `effort` under
[Roles and `.orbit/config.toml`](#roles-and-orbitconfigtoml)). Picking a role sets the model, tool
access and effort to that role's defaults, except those you've already picked in this edit. `j`/`k`
pick a field, `J`/`K` scroll the prompt box one line at a time (whichever field is selected), `i`
types in the title or prompt (`esc` stops; `enter` stops in the title and adds a line in the
prompt), `enter` on role, model, tools or effort opens a picker, `ctrl+s` saves (from anywhere,
even while typing) and `esc` discards. A task needs a title; the prompt may be empty. `enter` on a
running or verifying task, or on a review orbit queued, only shows why it can't be edited.

The task list belongs to the open session, which orbit saves as you go (see [Sessions](#sessions)).
It keeps the topmost running or verifying task in view, scrolling just enough to show it with the
row above it, and only when it would otherwise be out of view; activating the list with `enter`
starts the highlight on that task. Activating the list, or scrolling it with the mouse wheel,
holds it: it stops scrolling by itself and its title reads `Task list · held`. The first `esc`
leaves the list still held, with its highlight where it was; `esc` again, with the Task list
selected, lets go: the list scrolls to the running task at once and follows it again. Switching
sessions lets go too, leaving list mode if the list was active.
[docs/task-list.md](docs/task-list.md) records the requirements.

A task whose role runs in a lane other than `work` shows the lane as a dim tag after its title
(`2. ⏳ Review the change  review`); a coder task shows none (see `lane` under
[Roles and `.orbit/config.toml`](#roles-and-orbitconfigtoml)). With the list active, `L` narrows
it to one lane: all tasks, then each lane its tasks run in, in the order the roles are listed,
then all again. While narrowed, only that lane's tasks show, still numbered as on the whole list,
and the title names the lane: `Task list · review`, or `Task list · held · review` while held.

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
`c` on an agent to cancel its work: for a reviewer, or any agent in a lane that starts by itself
(see `lane` under [Roles](#roles-and-orbitconfigtoml)), just its task (the lane's next one
starts); for the planner, just its turn; for the coder, the same as `ctrl+s`.

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
**reviewer**, **super-reviewer** and **configurator**, and one for each other conversational role
the project configures (see `conversation` under
[Roles and `.orbit/config.toml`](#roles-and-orbitconfigtoml)), such as **architect**. The pinned
tabs are there from the start and keep the whole session's history; every agent in a role shares its tab (two reviewers running at
once both show under **reviewer**), each message headed and every other entry prefixed with its
agent's id, so you can tell them apart. Whether the speaker changed is judged within the tab: under
All, where agents interleave, messages are headed more often. The planner's messages are never
headed. Agents in any other role, such as the startup check (`checker`) or a custom role from
`.orbit/config.toml`, get a temporary tab of their own after the pinned ones, labelled by role
(`checker`, `checker 2`, …), which closes when the agent exits; their entries stay under All. A
role's tab also shows its tasks' headers (`Task 2: …`), verification results and merge notes, so a
coder task's whole run reads under **coder**; those of a task in a role without a pinned tab, and
the startup check's, show only under All, as do notices. Click a tab to switch, cycle with
`tab`/`shift+tab`, or jump to the planner tab with `ctrl+t` (again goes to each other
conversational role's tab in turn, then back). A role's tab starts with `●` while one of its agents
is running (a conversational role's while it's answering), and a tab with output you haven't seen,
a task note included, ends in `*`. The prompt talks to the conversational role whose tab is shown,
and to the planner on any other tab: its placeholder names the role (`Press i to talk to
Planner, esc to stop typing`), its title reads `Prompt · <role>`, and the settings under it are
that role's.

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
counts are rounded as in a turn's rule (see above). While the session's run waits for a coder
(see [Taking turns](#serving-the-control-plane)), the task about to run says so after its settings,
with how many coders hold the slots: `⏳ waiting: 3 coders busy`, or `⏳ waiting for a coder` if
that isn't known; it goes once the task gets a coder. A failed task doesn't stop the rest, but for
two: a fix task that ends in error stops the run, so the work it fixes stays in the coder's tree
until the fix runs again, and so does a task whose agent hit Claude's usage limit or found
Claude's API not answering (see below);
the next run starts with the task it stopped at, ahead of any that failed before it, and any
run, even after switching sessions or reopening orbit, starts with a fix that ended in error or
was cancelled;
`ctrl+s` cancels the running task (stopping
its agent) and skips the remaining ones. Running again skips tasks that completed, failed
verification or were given up on, and editing a task makes it pending again. Tasks added while a
run is going join it; the running task can't be edited.

**Usage limits.** When an agent's error says it hit Claude's session or weekly limit (`You've hit
your session limit · resets 6pm (UTC)`), the run stops there rather than failing every task after
it, and starts again by itself: a minute after the reset time the usage monitor last fetched for
the limits used up, or, without one (as with `--no-usage`, or before the usage caught up), 15
minutes later, when a limit still reached stops it again at once. The conversation says when
(`Claude's usage limit is reached, so the run stopped. It starts again by itself at Wed 21 Oct
18:01 UTC; ctrl+r starts it now and ctrl+s cancels that.`) and again as it starts. The task that
hit the limit runs again first, afresh, as any rerun does. While the run waits, `ctrl+r` starts it
at once, as does a confirmed `ctrl+e`, configurator first; `ctrl+s` cancels the wait (`The run
won't start again by itself.`), and switching sessions or quitting drops it. [docs/00016-bugfix-fix-chains-and-usage-limits.md](docs/00016-bugfix-fix-chains-and-usage-limits.md)
records the requirements.

**API outages.** When an agent's error says Claude's API isn't answering (`API Error: 500`,
`502`, `503`, `504` or `529`, `overloaded_error`, `API Error (Connection error.)` or `Request timed
out`; not a client error such as `400` or `401`), the run stops there in the same way, and starts
again by itself after a backoff: 2 minutes, then 5, 10, 20, and every 30 minutes from then on, with
no limit. The backoff starts over at 2 minutes as soon as any task's agent finishes its run without
such an error, however its task ends. The conversation says when (`Claude's API isn't answering
(API Error: 529 Overloaded), so the run stopped. It starts again by itself at Fri 23 Oct 14:20 UTC;
ctrl+r starts it now and ctrl+s cancels that.`) and again as it starts (`Starting the run again now
Claude's API may be answering.`). The task it stopped at runs first, and a fix carries on from the
coder's tree, as at the usage limit; `ctrl+r`, `ctrl+s`, switching sessions and quitting work on the
wait as they do there. Reviews and planner turns don't pause: a planner turn ends in error saying
so in place of the API's own text (`Claude's API isn't answering, so the planner couldn't reply;
send your message again later.`), and nothing is sent again by itself.
[docs/00023-bugfix-pause-runs-when-claude-is-down.md](docs/00023-bugfix-pause-runs-when-claude-is-down.md)
records the requirements.

While a run waits to start again, after a usage limit or an outage, a line by the prompt says so,
with the time it starts again in local time: `run paused: Claude's API isn't answering; trying
again at 14:20` for an outage, or `run paused: usage limit; starts again at 18:01` for the usage
limit (`run paused; starts again at 14:20` for a reason this orbit doesn't know, from a newer
server). The session's row in the picker says the same. The line goes once the run starts again,
`ctrl+s` cancels the wait, switching sessions drops it, or a run you start replaces it.

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
workspace's directory. Nothing waits for it: the prompt, the planner and `ctrl+r` all work meanwhile.
The verification panel shows its progress under a "Startup check" heading, with the same icons and
timing as a task's verification, until a task starts and its own verification takes the panel
over. It's listed in the agents pane as `startup-check`, where `c` cancels it. When it finishes,
the conversation notes `✅ Startup check passed`, or on a failure the step that failed (`setup`
if the setup command did) and its output, with a 📝 draft task "Fix failing check:
<step>" whose prompt holds that output (none while the `coder` is disabled), and a reminder that
`ctrl+e` reconfigures the steps if they're what's wrong. `--no-verify` skips it, as does having no steps.

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
chain. A fix carries on in the tree the task it fixes left; if that work is gone from the tree (no
changes, and no commits the base branch lacks, as when it was put aside), the fix gives up before
an agent starts (`Gave up: The work it fixes isn't in the coder's tree any more; look for it in git
stash list.`) and the task it fixes stays ❗.

Verification is skipped for tasks run with tools `off`, for tasks that changed no files (but for a
fix task, whose passing completes the task it fixes, so it's always verified), and for an
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
line above. A decision that's refused, or can't be sent, leaves the conversation and its proposal's
window comes back, next if another is showing, so you can decide again. The agent revises up to `fix_attempts` times. If it's cancelled or exits while its proposal is showing, the window
closes and the task is cancelled. When several agents propose checks, their windows come one at a
time, in the order they proposed.

Accepted checks take effect at once. Orbit itself (not the agent) writes them into
`.orbit/config.toml`, replacing its `[[verification.steps]]` and `[setup]` and keeping every other
key and comment, creating the file if it's missing. In the worktree the change is committed as
"Configure verification steps", with the steps listed in the body, and lands on the base like any
task's work; with `--no-worktree` the file is written in the workspace's directory and not
committed.
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

A commit is reviewed, and counts toward a super review, only once it has merged into the base: work
that stays on `orbit/coder`, whether it couldn't be merged or its base kept moving, is reported to
review as ended without landing a commit.

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
| The store | the state directory | `orbit.db`, the saved sessions, their event logs, the super review count and the record moves of every repository, `locks/`, and `fallback/` for events a failed write left unsaved (see [Storage failures](#storage-failures)) |
| Config files | `.orbit/config.toml` in the repository | the verification steps, setup, roles and limits, committed with your code |
| User config | `config.toml` in the config directory | your own settings for every repository, such as `[storage]`, `[server]`, `[remotes]`, `[telemetry]`, and in `[roles]` your machine's model and effort for roles and your personal roles, with the instructions files they name |
| The token | `token` in the config directory | what frontends show `orbit serve` to connect (see [Serving the control plane](#serving-the-control-plane)) |
| Certificates | `certs/` in the config directory | the CA, server and client certificates and keys `orbit certs` makes for TLS and mutual TLS (see [Serving the control plane](#serving-the-control-plane)) |
| The run file | `run/server.json` in the state directory | where the `orbit serve` running on that state directory listens, beside `run/serve.lock`, which that serve holds |
| Runtime-owned | `~/.claude` | Claude Code's own: your login, read for usage limits, and the agents' conversations |

The state directory is `--state-dir`, else `$ORBIT_STATE_DIR`, else `$XDG_STATE_HOME/orbit`, else
`~/.local/state/orbit`. Orbit creates it readable by you alone, and `orbit.db` likewise. One
database serves every repository you run orbit in, each with its own sessions, found by the
repository's path. Session locks are lock files in `locks/` there, so two orbits on one repository
keep out of each other's sessions only when they share the state directory.

A repository that's moved keeps its sessions. Orbit records each repository's root commits and
its `origin` URL; when it starts on a path it doesn't know (a local `orbit chat`, `orbit serve`,
or a frontend adding a workspace), it looks for a workspace with the same root commits, and the
same `origin` or none on one side, whose path has gone. The `origin` matches however it's
written: `git@github.com:o/r.git`, `ssh://git@github.com/o/r` and `https://github.com/o/r` are
the same. If exactly one is found, the new path takes it over, with its sessions and events, and
the first frontend to attach is told `Re-attached the sessions of <old path>, which moved to <new
path>.` A clone orbit made, taken over by a path it didn't clone into, is no longer managed, so
purging it later leaves that directory alone. A second clone while the first is still where it was gets a workspace of its own, as
does a repository with no commits yet; if several gone workspaces match, none is taken and the
note says `<new path> looks like <old a>, <old b>; started it as a new workspace.`

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

`orbit db forget PATH` deletes everything orbit keeps of the workspace at `PATH`, or the repository
`PATH` is in, as `orbit chat` finds it (or with the id `PATH`): its saved sessions and their events, its review count, its record moves, its turns' usage, its session
locks in `locks/`, its fallback files, in the state directory and the repository, and its entry, so orbit starts afresh
there next time; for a clone `orbit serve` made, it deletes the clone's directory too, and never a
directory orbit didn't clone into. Files go before the records, so a file it can't delete leaves
the workspace registered, and running it again finishes the job. A clone's directory is first
moved aside, to `.<name>.purging` beside it, and deleted there, so one only partly deleted is
finished off too. It says what it deleted, such as `Forgot /home/me/code/old:
deleted 3 sessions and 0 fallback files.`, and takes `--state-dir` as `chat` does. It refuses,
with status 2 and deleting nothing, a path no workspace is registered at, a workspace the `orbit
serve` running on the state directory serves (remove it there with `purge`, or stop the server),
one whose lock another orbit holds (`Can't forget /home/me/code/old: an orbit chat, pid 4242 holds
its lock.`), and a clone holding work that exists nowhere else, listing it, as a frontend's purge
does.

### Storage failures

When a write to the store fails, whether the event log, the session record (taking its lock on
the first save included), the super review count or the record moves, orbit halts the session instead of carrying on with work it can't record: the running
agents, the planner's turn, the reviews and the startup check are cancelled, and every command
but quitting and "Try again" is refused with `storage_failed` and the same explanation. Nothing more is written
to `orbit.db` until storage is resumed.

The explanation, shown in the conversation as an error, says which write failed, the file it
failed on (the database, or the session's lock file when taking the lock failed) and the error
SQLite or the operating system reported, what orbit makes of it and what to do: a full disk needs free
space; a read-only database or a permission error means checking the owner and mode of the state
directory; a database locked beyond the busy timeout means another process holds it; a missing
file means the state directory was moved or removed. An error orbit doesn't recognise is said to
be one. It also gives the `seq`s of the events that weren't saved and where they went, says that
what was saved is intact up to the last saved event, and that "Try again" or restarting orbit
resumes.

The app shows the halt in a modal titled "Session stopped: storage failed" with the whole
explanation: `enter` is "Try again", which stays open until the halt lifts, then closes, or shows
the explanation again if a write still fails; `j`/`k` (or the arrow keys) scroll it and `esc` closes it. While the session is halted, a line
in red above the prompt says "Session stopped: event log failed; see the transcript" (or "session
record failed" or "review count failed", whichever write failed), until the halt lifts. Closed,
the modal opens again, with the refusal's explanation, whenever a command is refused with
`storage_failed`, such as `ctrl+r`; a session opened while halted opens it too. Quitting while the
session is halted prints the explanation to the terminal (stderr) once the app has closed.

The events that weren't saved, and every event after them while the session is halted, go to a
fallback file, one JSON envelope per line, readable by you alone:
`fallback/<workspace id>-<session id>.jsonl` in the state directory, or, if that can't be
written, `.git/orbit/fallback/<workspace id>-<session id>.jsonl` in the repository
(`.orbit/fallback/` outside git), which its worktrees share. The explanation names the file, or
says that neither place could be written.

Once the cause is fixed, "Try again" retries the writes that failed: the session record as it is
now, taking its lock first if that failed, a saved session's rename whose save failed (dropped,
with an error, if that session was deleted or another orbit has it open since), the super review
count, the record moves and the event log. When they work, the events in the fallback file and those still waiting
are appended to the log in `seq` order in one go, skipping any it already holds, the fallback file
is deleted and the halt is lifted: commands are accepted again, the reviews waiting on the board
start, a run the usage limit or an API outage had stopped starts again if its wait ended
meanwhile, and work can carry on; a startup check the halt cancelled stays cancelled, like one
you cancelled. Events that
reached neither the log nor a fallback file are recorded as a gap, which the conversation, and
paging back through it, shows in their place as a line such as "Events 1204–1210 weren't saved".
If a write still fails, or the store still can't list, lock or read the session a rename is
retried on, the session stays halted and the explanation is given again for the failure now.

Restarting orbit recovers the events too. The orbit that owns the repository, once the
database is open as it starts, imports each of the workspace's fallback files, from both places,
into the log the same way, skipping events already stored, deletes it, and says in the
conversation how many events it recovered from which file, as in "Recovered 37 events from
.git/orbit/fallback/<workspace id>-<session id>.jsonl into the session's log.", counting only the
events it newly stored; a file whose events were all stored already is deleted without a word. A
file an earlier orbit left in the repository as `.git/orbit/fallback/<session id>.jsonl` is
recovered only by the workspace that has that session, and left alone by the others. A fallback
file that can't be read or imported, or whose session can't be told because the saved sessions
can't be listed, stops orbit before the app starts, with status 2 and a message naming the file
and the error, leaving the file as it was. A read-only orbit leaves the
fallback files alone, as the owner may still be writing them, and recovers them the same way
once it takes the repository over, before running the work queued; a file it can't recover then
is reported as an error and left for the next start.

### Planning with the planner

On any tab but another conversational role's, the prompt talks to the planner (its title reads
`Prompt · planner`). It wraps what you type and
grows with it up to 8 lines, then scrolls, and shrinks again once sent; `enter` sends, and pasted
text keeps its newlines. Orbit is for planning, tasks and reviews, with no free-form chat besides.
The **planner** tab, always second in the strip, shows just the planner conversation: your
prompts, and the planner's messages, tool calls and exchanges with orbit (`ctrl+t` jumps there,
and pressed again goes on to any other conversational role's tab, then back to the tab you were
on). Tell it what you want to build; it asks
what's unclear in rounds of a few questions, each offering options with a recommendation, and
breaks the goal into small tasks, proposing each with `propose_task` (a title is required, and
kept to one line). Every plan's first task writes the plan's record: a numbered requirements
document in `docs/` (or wherever the project keeps such records), named like
`docs/00001-feature-dark-mode.md`, with the decisions you confirmed and the tasks that carry them
out. Every other task's prompt opens with `Plan: <path>` pointing at it, so coders and reviewers
read the same record. A task's record keeps its number unless another record has taken it by the
time the task runs, such as one planned in another session: orbit then gives the record the next
free number, in that task and in every task that names it and hasn't completed, and says so. The
planner hears the new path ahead of its next prompt, even after a switch or a restart, as the
session saves what it's still to hear. Orbit keeps each move in the store, so a task in any
session that still names the old path is pointed at the record where it's written, or else where
the move took it, or where the other tasks on the board name it. Its first prompt, and the first after the verification steps change, tells it
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
`default` picked, it uses the planner role's model, if `.orbit/config.toml` or your `config.toml`
sets one (see [Your machine's model and effort](#your-machines-model-and-effort)).

The planner's conversation is kept with its session, so it carries on across restarts and session
switches. `ctrl+n` resets it, after you confirm (`y` to go ahead, `n`/`esc` to cancel): to pick up a
change to the planner's role, or to clear a conversation grown too long. Orbit ends the planner's
conversation, and its access to orbit, so the next prompt starts a fresh one, told the
verification steps again, and saves the session without it. It reads `[roles.planner]` from
`.orbit/config.toml` again, with the model and effort your `config.toml` sets for it, so a change
to the planner's instructions, tools, tool access, model or effort takes effect; other roles and
settings stay as they were. If either file is invalid, the planner keeps its role and orbit shows
the problem, but still resets the conversation. Draft tasks stay on the task
list, the planner tab keeps its history, and the conversation shows a dim `── planner reset ──`
rule under the planner tab and All. The reset is refused while the planner answers (stop it with
`ctrl+x` first), and before its first prompt there's nothing to reset.

### Reviews

Every commit from one of your tasks that merged into the base is reviewed in the background while
the next task runs (one that couldn't be merged isn't, and doesn't count toward a super review). Orbit
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
is paused); it resumes with the next commit from one of your tasks. A review may suggest at most `review_suggestions` tasks (default 5), and a suggestion is
refused when any task on the board, whatever its status, or any earlier suggestion of the session
has the same title (compared on one line, ignoring case and spacing; a reworded repeat gets
through). Each reviewer is also told the titles of the tasks still queued or running, so it can
steer clear of them (other than the reviews and configurator tasks orbit queues for itself). With the defaults, one of your commits can lead to at
most 15 suggested tasks. Removing a suggested task drops it from its batch, which is then reviewed
without it (or closed, if nothing in it landed); removing a queued review closes the batch it would
have suggested into.

Every `super_review_every` commits (default 10, counting your own and the planner's commits, not
those of suggested tasks), a `super-reviewer` (Opus by default) reviews the **whole codebase** at the
latest commit, looking for duplication, structure that no longer fits and gaps in tests, and may
suggest up to `super_review_suggestions` tasks (default 10). Those form a batch at round 1 and carry
on like any other review's. Each review ends with a one-line verdict that also counts the findings
its cap left out, so `5 suggestions, 2 more left out` tells you a second pass would find more.
`ctrl+g` asks for one straight away and restarts the count. The count belongs to the repository
rather than to a session, so it carries on across sessions and restarts. A super review that falls
due waits while review is paused, and while suggested tasks (or reviews) are pending or running; it
comes with your next commit once none is.

The reviews orbit queued can't be edited: remove one instead. Reviews run in the reviewer's lane,
`review`, which starts its tasks by itself: a `reviewer` task you add starts there at once too.
Reviews need the worktree, and a reviewer in a lane of its own; `--no-review` turns them off, and a
`reviewer` task you add then runs in its lane on a read-only snapshot of the base commit.

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

The built-in roles are data shipped with the package in
[`src/orbit/agent/external/instructions/`](src/orbit/agent/external/instructions/) and read when
orbit starts: `roles.toml` has a `[roles.<name>]` table for each, with the fields above, and its
`instructions` name the Markdown file beside it that the role is told (for example `coder.md`). A
project can still replace a role's instructions with `instructions` in `.orbit/config.toml`: one
line ending in `.md` is the path of a Markdown file under `.orbit/` whose text the role is told
(`instructions = "roles/coder.md"` reads `.orbit/roles/coder.md`), and anything else is the text
itself. Only a file a role's `instructions` names is read, so a `.orbit/roles/reviewer.md` that
none names changes nothing; one that's missing or can't be read is refused, naming the file and
the role.

A role also says which lane runs it, where it works, whether you talk to it, whether its work
merges, whether it's enabled and how hard its agents think:

- `lane`: the lane that runs the role's agents, a plain name of letters, digits, `-` and `_`. Each
  lane of the enabled roles that aren't conversational runs one task at a time, the next on the
  task list whose role is in it, beside the others. The lane whose roles merge (`work` for the
  built-in roles; `work` too if none does) runs its tasks when you press `ctrl+r`, and `ctrl+s`
  holds it. Every other lane, such as `review`, starts a task as soon as one is queued for it
  (added, accepted or edited), and `ctrl+r` runs what waits there too; stopping it, with `c` in
  the agents pane or the protocol's `CancelTurn`, stops just its task, and the lane goes on with
  the next. While the work lane waits out Claude's usage limit or an outage, no lane starts a
  task; they start again with it, or once `ctrl+s` cancels its wait. Each lane shows in the
  Snapshot's `busy`, by name. On the task list, a task in a lane other than `work` is tagged with
  its lane, and `L` narrows the list to each lane in turn (see [Tasks](#tasks)).
- `workspace`: where its agent works: `worktree` (the coder's separate worktree, the only one whose
  work can merge), `snapshot` (a fresh read-only snapshot of the base commit, made for each run and
  dropped after it), `checkout` (your checkout, read in place) or `none` (an empty temporary
  directory, made for each run and removed after it, however the run ends). Outside a git
  repository, or with `--no-worktree`, `worktree` and `snapshot` work in place. Tasks in
  a role with `workspace = "worktree"` count toward `max_coders` (see Taking turns under
  [Serving the control plane](#serving-the-control-plane)).
- `conversation`: whether you talk to it, as you do the planner, rather than it taking tasks.
  Each enabled conversational role has a seat of its own: its own conversation, which the session
  saves and carries on when you open it again, and which resetting it ends alone; its own model,
  tool access and effort, saved with the session; and its turns in its own lane, so it answers
  while the planner does. It's listed as conversational in the Catalog, and its lane in the
  Catalog's lanes and the Snapshot's `busy`. Each has a pinned tab after the built-in roles':
  `ctrl+t`, which with the built-in roles alone toggles the planner's tab, goes to the planner's
  tab, then each other conversational role's in turn, then back to the tab you were on. On a
  conversational role's tab, the prompt talks to that role, `ctrl+x` stops
  its turn, `ctrl+n` resets it (its role isn't read again, unlike the planner's), and `ctrl+o`,
  `ctrl+l` and `ctrl+y` pick its settings. The protocol's `SendMessage`, `ResetConversation`,
  `CancelTurn` and `UpdateSettings` reach it too (see [docs/protocol.md](docs/protocol.md)).
- `merge`: whether its finished work is committed and merged.
- `enabled`: whether it runs at all. A disabled role is left out of the Catalog's agents, and its
  lane out of the Catalog's lanes unless an enabled role shares it. A disabled `reviewer` queues no
  reviews as work lands, and a disabled `super-reviewer` no super reviews, however many commits
  land; a disabled `super-reviewer` also takes the `super-review` trigger out of the Catalog,
  hiding `ctrl+g`, as a disabled `configurator` does `configure-checks` and `ctrl+e`, and firing
  either is then refused as an unknown trigger. A message to a disabled `planner`, or resetting its
  conversation, is refused (`planner is disabled in this project's configuration.`), as are adding
  a task in a disabled role or editing one into it, naming the role, and `ctrl+r` while a task in
  one is still to run. Nothing runs such a task, which stays on the task list until you edit or
  remove it, and a failing startup check adds no draft while the `coder` is disabled.
- `effort`: how hard its agents think: `low`, `medium`, `high`, `xhigh` or `max`, the levels
  `claude --help` lists; any other value is refused, listing those. Left out, the CLI's default.
  Like its model and tool access, it's the default for tasks orbit queues in the role, and what
  the task editor fills in when you pick the role, and is saved with them and the session. An agent runs with its task's effort, or for a conversational
  role its session's, and otherwise with its role's, passed to the CLI as `--effort`; with none,
  no flag is passed and the CLI's default applies. `CLAUDE_CODE_EFFORT_LEVEL` is removed from
  every agent's environment, so one set where orbit runs never overrides orbit's setting or the
  CLI's default.

In the TUI, the task editor's **Role** field picks a task's role among the enabled roles that
aren't conversational, each shown with its lane (`Coder · work`) and a personal role marked
`personal` (`helper · helper · personal`), and fills in the role's model,
tool access and effort; its **Effort** field sets the task's own effort, or leaves it unset for the
role's (see [Tasks](#tasks)). `ctrl+y` picks the effort of the conversational role whose tab is
shown, or the planner's elsewhere, as `ctrl+o` and `ctrl+l` pick its model and tool access.

The `coder` works in lane `work` in a `worktree` and merges; the `configurator` works in `work` in
a `worktree` and doesn't merge; the `reviewer` and `super-reviewer` work in `review` on a
`snapshot`; the `planner` works in `planner` in your `checkout`, in conversation; all are enabled,
and none sets an effort.
A role whose fields don't go together is refused, naming the role and the rule: `merge = true`
needs `workspace = "worktree"` and `verify = true`; `verify = true` needs `merge = true`, as checked
work that doesn't merge would vanish once put aside; `workspace = "checkout"` can't have
`tool_access = "full"`, as the agent could edit your tree; `conversation = true` needs
`propose_task` in its tools and `get_task` left out, `merge = false`, `verify = false` and a
workspace other than `worktree`; and no role that isn't conversational may share a conversational
role's lane. There's one worktree, so every enabled role with `workspace = "worktree"` must share
one lane, or the config is refused, naming the roles and their lanes.

An agent calling a tool its role doesn't have gets an error reply (the CLI only allows its role's
tools, too, and orbit's others are disallowed so the agent doesn't see them); an agent still
working when its role's time runs out is stopped and the task fails. The planner's limit applies to
each of its turns: one that runs out of time is stopped and ends in error, and the next prompt
carries on the conversation.

A project can change these, orbit's limits, its verification steps (see
[Verification steps](#verification-steps)) and its worktree setup (see
[Where tasks run, and how their work lands](#where-tasks-run-and-how-their-work-lands)), in
`.orbit/config.toml`. A role it names that's built in keeps the fields it doesn't set; any other
name adds a role, which must set `instructions` and otherwise gets `get_task`, `complete_task`,
`report_progress` and `give_up` as its tools, `restricted` tool access, the default model, its
claims verified, no time limit, the coder's lane `work`, `worktree`, no conversation and
enabled, and no effort. It merges, being verified and in a worktree; one in another workspace must
set `verify = false`, and one with `verify = false` doesn't merge, and can't be made to. Every other field is optional:

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
instructions = "roles/coder.md"  # the text of .orbit/roles/coder.md

[roles.docs-writer]
instructions = "You write and update the project's documentation."
tools = ["get_task", "complete_task", "report_progress", "give_up"]
tool_access = "full"
verify = false
```

`orbit chat` reads it at startup, and `orbit serve` at startup and as a frontend adds a workspace;
each refuses an invalid one, naming each problem (for example
`roles.coder.tool_access: Input should be 'off', 'restricted' or 'full'`).

#### Your machine's model and effort

Which models and how much effort a machine can run is yours to say, not each project's: the
`[roles]` table in your `config.toml` in the config directory (see
[Where orbit keeps things](#where-orbit-keeps-things)) sets `model` and `effort` for every role,
and a `[roles.<name>]` table sets them for one role, in every repository orbit runs in:

```toml
[roles]
model = "haiku"   # every role's model
effort = "low"    # every role's effort

[roles.planner]
model = "opus"    # the planner's
```

Each of a role's fields comes from the most specific place that sets it: the project's
`.orbit/config.toml` for that role, then your `[roles.<name>]`, then your `[roles]`, then the
roles shipped with orbit, then orbit's defaults. So above, every role runs on `haiku`, the
`super-reviewer`'s `opus` from the package included, unless the project sets a role's model; the
planner runs on `opus` unless the project sets the planner's; and a project's
`[roles.planner] model = "sonnet"` wins over both. Without a `[roles]` table, roles are just the
project's over orbit's. A `[roles.<name>]` table that sets only `model` and `effort` for a role
neither orbit nor the project has changes nothing; one that sets any other field defines a
personal role (see [Your personal roles](#your-personal-roles)).

Only `model` and `effort` of orbit's own roles belong to your machine: any other field of one,
such as `[roles.coder] verify = false`, is refused, naming the file, the key and that it belongs
in the project's `.orbit/config.toml`, so your machine can't turn off a project's verification;
so is any other field in `[roles]` itself. `effort` takes the levels a role's does, and any other
value is refused, listing them. `orbit chat` reads
your `config.toml`'s roles as it opens its workspace, and `orbit serve`, with the config directory
of the account it runs as, as it opens each workspace it serves; both read them again, with the
project's, when the planner is reset. An invalid one is refused as an invalid
`.orbit/config.toml` is: it stops `orbit chat` with status 2, and a workspace `orbit serve` can't
open.

#### Your personal roles

Roles of your own go with you to every repository: a `[roles.<name>]` table in your
`config.toml` naming a role orbit doesn't ship, and setting more than its `model` and `effort`,
defines a **personal role**, available in every workspace orbit opens with that config directory.
It may set every field a role in `.orbit/config.toml` may, and is held to the same rules: like a
role a project adds, it must set `instructions` and gets the same defaults for the rest (the
coder's lane `work`, `worktree` and so on), and your `[roles]` `model` and `effort` apply beneath
its own. Its `instructions` naming a Markdown file are read from the config directory:

```toml
[roles.helper]
instructions = "roles/helper.md"  # the text of roles/helper.md in the config directory
lane = "helper"
workspace = "snapshot"
merge = false
verify = false
```

A personal role is checked with the rest of the workspace's roles, too: one that breaks a rule
across roles that the others keep, such as `workspace = "worktree"` in a lane of its own beside
the coder's, or a task role in the planner's lane, is refused naming your `config.toml`, as an
invalid one of its own is (a missing instructions file names both files and the role). The
Catalog marks it `personal`, and the task editor's **Role** picker shows it as
`<label> · <lane> · personal`.

A project's `.orbit/config.toml` can always define a role of the same name: the project's
replaces yours entirely, none of your role's fields carrying over (your `[roles]` `model` and
`effort` still apply beneath it, as to any of the project's roles), and the workspace's startup
notices say so: `The project's role helper replaces your personal role of that name.`

### Seeing the configuration in effect

`orbit config show` prints the configuration orbit resolves for a workspace (the current
directory's, or the one `-C PATH` names, picked as `orbit chat` picks it) from this machine's
files, each value with the layer it came from: the limits, each role field by field, the
verification steps, the setup, and your `config.toml`'s `[storage]`, `[server]` and
`[telemetry]` (the config directory is `--config-dir`, as for `orbit chat`). It needs no running
`orbit serve`, and writes nothing. Each value is a line, `<key> = <value>   <layer>`, the value as
JSON, with the limits, each role and each other section in groups of their own:

```
$ orbit config show roles.reviewer
roles.reviewer.instructions = (reviewer.md, 64 lines)   package
roles.reviewer.model = "sonnet"   project .orbit/config.toml
roles.reviewer.tool_access = "restricted"   package
...
roles.reviewer.effort = "low"   user ~/.config/orbit/config.toml
```

The layers, least specific first:

- `default`: orbit's own default, in no file.
- `package`: the roles shipped with orbit (see [Roles and `.orbit/config.toml`](#roles-and-orbitconfigtoml)).
- `user <path>`: your `config.toml` in the config directory.
- `project <path>`: the workspace's `.orbit/config.toml`.

A file inside the workspace shows relative to it, and one in your home directory from `~`. A
role's instructions show as the file they're in and their length, `(reviewer.md, 64 lines)`, or
`(inline, 3 lines)` for text the file gives itself; `--full` prints their text instead, between
lines of `"""`. A `KEY`, dotted as in the TOML it comes from, shows only the settings at or under
it: `roles` every role's, `roles.reviewer` one role's, `server.port` one value and
`verification.steps.1` the first step's. `--json` prints the same as one JSON object, nested by
the parts of each key, each value an object `{"value": …, "layer": …, "file": …}` of its value,
its layer's name (`default`, `package`, `user` or `project`) and that layer's file, its whole
path, or `null` for `default` and `package`. A `KEY` that names no setting stops it with status 2
naming the key, and an invalid configuration with status 2 and the message `orbit chat` gives.

### How agents reach orbit

Orbit starts its MCP server the first time a task runs, on a random port on `127.0.0.1`. Each agent
gets its own unguessable token, sent as `Authorization: Bearer …` with every call; orbit ignores calls
whose token it didn't issue, and revokes a token when its agent's run ends. The token is passed to
the CLI in the `ORBIT_AGENT_TOKEN` environment variable, which the MCP config references, so it never
appears in the process list. Agents are started with `--strict-mcp-config` (only orbit's server),
`--allowedTools` naming the role's orbit tools (allowed at every access level) and
`--disallowedTools` naming orbit's others,
`ENABLE_TOOL_SEARCH=false` (orbit's tools are loaded up front),
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (your Claude Code auto-memory, which worktrees share with the
repository, stays out of agents' context), and without `CLAUDE_CODE_EFFORT_LEVEL` (an agent's effort
is orbit's `--effort`, or the CLI's default; see `effort` under
[Roles and `.orbit/config.toml`](#roles-and-orbitconfigtoml)).

A model picked with `ctrl+o` applies from the planner's next prompt and keeps its conversation
(with `default` picked, the planner uses `[roles.planner] model` if the config sets one), as does
an effort picked with `ctrl+y`; the tool access picked with `ctrl+l` is what new tasks start with
in the task editor. On another conversational role's tab, the three pick that role's settings
instead, leaving the planner's as they were. All are saved with the session. `ctrl+l` does not
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
limiting, network errors), the panel says why and greys out the last numbers. The panel itself
is display only, but the latest usage it fetched also decides when a run stopped at the usage
limit starts again (see **Usage limits** under [Tasks](#tasks)).

`--no-usage` turns it off, and orbit then never reads the token.

### Serving the control plane

`orbit serve` runs orbit's backend, the control plane, on its own: it serves the protocol (see
[docs/protocol.md](docs/protocol.md#transport)) over a WebSocket, and the work goes on with no
frontend connected (to host it in a container, see
[Hosting in a container](#hosting-in-a-container)). One server serves many projects, its
workspaces, each with its own sessions, open session, work and lock, and it isn't tied to the
directory it starts in: it serves every workspace added with `--add PATH` (repeatable), now or by
an earlier `orbit serve` on the same state directory, so a plain `orbit serve` serves them all
again, and with none added yet it starts empty and says so. `--add` takes a project's directory, or
any directory in a git repository for the repository's top level; a path that doesn't exist, or
isn't a directory, stops it with status 2, as does one added now that can't be served (an
invalid `.orbit/config.toml`, no git identity for commits, a lock that can't be made or sessions
that can't be opened or imported), which is left as it was rather than added, as are those added
with it. A workspace added
earlier whose directory has gone since, or that can't be served for any of those reasons, is
skipped, with a warning naming it and why, and the rest are served. Before it listens, each
workspace served opens its latest session and starts its startup check and its tries of the
lock, with no frontend needed; a store that fails as one opens stops it with status 2. Frontends add and remove workspaces as it runs, with the protocol's
`AddWorkspace` and `RemoveWorkspace` (see
[docs/protocol.md](docs/protocol.md#workspaces)): adding takes an absolute path on the server's
host, a repository's top level stands for any directory in it, and the workspace opens its
latest session, takes its lock and runs its startup check, as at start; adding one already
served changes nothing. Removing one lets its lock go and keeps its sessions for when it's added
again; it's refused while the workspace runs work or another frontend views one of its
sessions, and if the store fails to record that it's removed, it stays served. A plain `orbit
serve` later serves those added, not those removed. A local `orbit
chat` works on its own repository alone and refuses both.

**Cloning.** A server can also be given a repository by URL, with no one touching its
filesystem: `orbit serve --add <url>` clones it before serving, and a frontend's `AddWorkspace`
with a `url` is accepted at once and cloned in the background, its progress (`Receiving objects
42%`) reported to every frontend, then served like any added workspace. It takes `https://`,
`ssh://` and `file://` URLs, scp-style `user@host:path`, and, from a frontend, an absolute path
on the server's host, which is cloned rather than served in place (`--add` serves a path in
place). Clones go to `clone_dir` in `[server]`, by default `workspaces` in the state directory,
each in a directory named after the URL's last part without `.git` (`proj` for
`git@github.com:o/proj.git`), or `proj-2`, `proj-3` and so on when that's taken; orbit marks
these workspaces as managed, clones it made. A clone that fails says git's message (`orbit serve
--add` stops with status 2), and one a frontend added that then can't be served says why,
both starting `Couldn't clone <url>: `; either leaves nothing behind, as does a clone `orbit serve --add` made
that it then can't serve, along with every other clone it made that run; one still under way
when the server stops is stopped and removed. The same URL can't be cloned twice at once.
`--add <url>` clones every time it's given, even a URL an earlier run cloned and still serves,
so after the first run start the server with a plain `orbit serve`, which serves its clones
again. In the TUI, the
workspace picker shows a clone's progress in a row of its own (see [Workspaces](#workspaces)).

**Watching what runs.** A frontend that shows more than one workspace at a time can watch
the control-plane stream, with the protocol's `Watch(["control_plane"])` (see
[docs/protocol.md](docs/protocol.md#the-control-plane-stream)): it's told every agent running in
any workspace served (its workspace, session, role and task), again whenever one starts or
exits, and what turns have cost in each workspace and in all, today (since midnight in the
server's local time) and over its lifetime, again after each turn finishes. `Watch([])` stops
it; a frontend that never watches is sent neither.

**Purging.** A frontend removing a workspace can have its records deleted too, with
`RemoveWorkspace`'s `purge`: under the same refusals as removing, its sessions, their events, its
review count, record moves, session locks, fallback files and registry entry go, so adding it again
starts afresh, and so does a managed clone's directory. A directory orbit didn't clone into is never
deleted. A clone holding work that exists nowhere else, uncommitted changes, stashes or commits on
any branch, or on a detached HEAD, that its remote doesn't have, isn't purged: the refusal lists
what would be lost (`1 commit on main not on its remote`), and nothing is removed until it's pushed
or dropped. Nor is a workspace another orbit owns, such as a local `orbit chat` in its repository
holding its lock while the server serves it read-only: the purge is refused as `busy`, naming the
owner (`Can't purge <path>: an orbit chat, pid 4242 owns its repository.`), before anything is
removed, though removing it without purging still works, and once the server takes the repository
over it's purged like any other. The server holds the workspace's lock until the purge is done, so
no local orbit starts on it meanwhile. Its files go before its records, so if one can't be deleted
the workspace is removed but stays registered, and the refusal says how to finish (`Removed <path>,
but couldn't purge it: …; run orbit db forget <path> to finish.`): the server no longer serves it,
so purging it there again can't, but `orbit db forget` on the server's machine finishes the job,
even while the server runs, and even for a clone's directory left partly deleted, which is moved
aside to `.<name>.purging` before it's deleted. A workspace no server serves, such as one only a
local `orbit chat` used, is purged with `orbit db forget` too (see [Where orbit keeps
things](#where-orbit-keeps-things)). In the TUI, `x` in the workspace picker offers `Delete its
sessions too` beside keeping them (see [Workspaces](#workspaces)).

Git clones as the server's account, with its credential helpers and SSH agent, and never
prompts: `GIT_TERMINAL_PROMPT=0`, and SSH runs with `-o BatchMode=yes` added to any
`GIT_SSH_COMMAND` or `core.sshCommand` you set. Frontends never send credentials, so a private
repository needs the server's account to have access to it already: an SSH key (in its
`~/.ssh`, or its agent) or a git credential helper (`git config --global credential.helper
store`, say). A clone that fails for want of them says so: `The server's account needs access
to this repository: an SSH key or a git credential helper.` It takes the backend flags `chat` has (`--model`, `--tools`, `--verify`,
`--worktree`, `--review`, `--usage`, `--profile`, which writes `serve.prof` and
`serve-blocked.log`), which apply to every workspace it serves, each reading its own
`.orbit/config.toml`, `--state-dir` and `--config-dir`, and:

- `--add`: a workspace to serve, by its path or a URL to clone (see **Cloning** above),
  repeatable.
- `--host`: the address to listen on, by default `127.0.0.1`. Without TLS only loopback is
  allowed (`127.0.0.1`, `::1`, `localhost`); another host stops it with status 2, pointing at
  `--tls`. With TLS any address is.
- `--port`: the port, by default 7433; 0 lets the system choose one. A port in use stops it with
  status 2, naming the port.
- `--tls`, `--cert`, `--key` and `--client-ca`: how it serves TLS (see **TLS** below).

In a git repository, unless `--no-worktree` is given, orbit commits the coder's work, so `orbit
serve` checks as it starts that git has an identity for commits in each workspace's repository: `user.name` and
`user.email` from the repository's, your global or the system git config, or the
`GIT_AUTHOR_*` and `GIT_COMMITTER_*` environment variables. Without one it stops with status 2
for a workspace added with `--add`, and skips one added earlier with a warning, saying to set them (`git config --global user.name "Your Name"` and `git config --global
user.email you@example.com`) or, in a container, to mount your `~/.gitconfig` into it.

Each workspace opens its most recently active saved session that no other orbit has open, or a
new one when there's none; frontends pick another in their session picker. Once listening it
prints one line with its URL, each workspace it serves and whether it owns its repository or is
read-only there (naming the owner), or that it serves none yet, and the token file (under mTLS,
the client CA), such as `Serving at ws://127.0.0.1:7433/: /home/me/code/orbit as its owner;
/home/me/code/other read-only, as an orbit chat, pid 4242 owns it. Frontends show the token in
/home/me/.config/orbit/token.`, then one line as each frontend connects (with the name its
`Hello` gives and its address, and under mTLS its certificate's subject and SHA-256 fingerprint)
and disconnects, and one as it stops; a connection closed without a `Hello`, like the probe `orbit
chat` makes to find a server, gets no line. Warnings and errors go to stderr.

One `orbit serve` runs per account and machine, or rather per state directory: while it runs,
`run/server.json` in the state directory, readable by you alone, holds its URL and process id,
and over TLS the CA a frontend on the same machine trusts and the hostname it connects by (see
**Finding the server** below); it's removed as it stops. From its start it also holds
`run/serve.lock` there, so that of two started together one alone serves. A second `orbit serve`
on the same state directory stops with status 2, saying `orbit serve is already running at <url>
(pid N); use --state-dir for a separate one.` (or `running on <state directory>` before the first
has written its run file); a run file left by a serve that didn't stop cleanly, whose process is
gone or is the new serve itself (as pid 1 in a container always is), is replaced.

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
| `/readyz` | 200 `{"status": "pass"}` | it's serving, with the store open, the workspaces loaded and storage working |
| `/readyz` | 200 `{"status": "warn", "output": "read_only"}` | another orbit owns the repository of a workspace served, so it's read-only there |
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
`chmod 600` that fixes it (under mTLS, which asks for no token, it's never read or made). Two `orbit serve`s making the token at once agree on it: the first to
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
way, how it serves TLS, where it clones repositories added by URL, how many coders run at
once and what frontends may browse; the flags override it. The server enforces `max_message_mb` on what
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
revoked = []         # SHA-256 fingerprints of client certificates refused under mtls
local_client_cert = "/home/me/.config/orbit/tls/local.pem"      # shown by chats here, under mtls
local_client_key = "/home/me/.config/orbit/tls/local-key.pem"   # its private key, chmod 600
clone_dir = "/srv/orbit/workspaces"  # where repositories added by URL are cloned
max_coders = 4       # coders running at once across the workspaces; 0 for no cap
browse_root = "/srv/repos"  # the one tree frontends may browse and add from; unset: anywhere
```

**Taking turns.** `max_coders` (4 unless set; 0 for no cap) caps how many tasks whose roles have
`workspace = "worktree"`, the coder's and configurator's among the built-in roles, run at once
across every workspace the server serves: tasks, fixes and startup-check fixes alike. Each task takes a coder's slot as its agent starts and gives it back once it has
ended, its verification and merge included, however it ends; while every slot is taken, it
waits in line behind those that asked before it, and a workspace's next task joins the back, so
workspaces take turns. A run waiting stays running, and its session's state says where it is
in line (`waiting_for_coder`, see [docs/protocol.md](docs/protocol.md#session-state)), which the
session picker, the line above the prompt of a session viewed read-only, the task list and the
workspace picker show (`waiting for a coder: 2nd in line`, `⏳ waiting: 4 coders busy`,
`waiting: 2nd`);
`ctrl+s` takes it out of line and holds it, as it stops any run. A run waiting follows its
board: it leaves the line as soon as its next task no longer takes a slot (removed, or one taking
none put ahead), running that task or finishing, and keeps its place, and then its slot, for a coder
task put ahead of the one it waited for. Reviews, super reviews, the planner and tasks of roles
working elsewhere don't count and never wait, and a run stopped at the usage limit or an API outage holds
no slot until it starts again. A local `orbit chat` serves one workspace and has no cap.

**Browsing.** Frontends can list the directories on the server's host, as its account sees
them, to find a repository to add (the protocol's `ListDirectory`, see
[docs/protocol.md](docs/protocol.md#workspaces)): repositories first, each marked, and the
workspace served at each, if any; the listing comes from the server, so it's the server's
filesystem a frontend on another machine browses. With `browse_root` set, an absolute directory,
frontends may browse and add workspaces by path within it alone, symlinks followed, so a link
inside it can't lead out, nor a path in a repository whose top level is outside it, and clone a repository on the server's host (an absolute path or a
`file://` URL) from within it alone too; remote URLs aren't fenced, and clones still go to
`clone_dir`. Unset, the server's account may browse
whatever it can read. Browsing starts in the directory holding the workspace added last (by
`AddWorkspace`, `--add` or a clone), if it's still there and inside `browse_root`; else at
`browse_root`, if set, else the server account's home directory. A local `orbit chat` refuses browsing.

Paths in `[server]` may start with `~`, and a relative one is taken from the config directory,
so `cert = "tls/server.pem"` names `~/.config/orbit/tls/server.pem`; relative `--cert`, `--key`
and `--client-ca` are taken from the directory `orbit serve` starts in. `browse_root` is the
exception: it must be an absolute path to a directory.

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
start: to renew it, replace the files (`orbit certs server` does) and restart `orbit serve`;
frontends reconnect on their own.

**Certificates.** `orbit certs` makes them all: a private CA, the server's certificate, and one
for each client machine under mutual TLS, bundled to copy to it; it renews them, lists them for
revoking, and leaves the CA's key wherever you keep it. (Or make them with openssl, as in
**Certificates with openssl** below.) To set up, `orbit certs init` makes a private CA and a
server certificate it signs, for the hostnames and IP addresses frontends reach the server by
(here `orbit.example.lan` and its address):

```sh
orbit certs init orbit.example.lan 192.168.1.20
```

It writes them to `certs/` in the config directory (`--config-dir` applies): `ca.pem` and
`ca-key.pem`, the CA, and `server.pem` and `server-key.pem`, the server's certificate and key, the
keys and `certs/` readable by you alone. The server certificate holds the names given, IP addresses
as such, then `localhost` and `127.0.0.1`, so chats on the server's machine reach it too; with no
names, it's for the machine's hostname. Keys are Ed25519; `--key-type p256` makes ECDSA P-256 keys
instead, for a browser or load balancer in front that needs them. The CA is valid for 10 years and
the server certificate for one, or `--days` (up to 3650; the CA stays 10 years). It leaves your
`config.toml` alone, printing the `[server]` lines to put there for mutual TLS (`tls = "mtls"`, and
`cert`, `key` and `client_ca` naming the files); for TLS without client certificates, use `tls =
"tls"`, with `ca` in place of `client_ca`. `ca-key.pem` signs certificates the server trusts, so
keep it secret (see **Keeping the CA's key offline** below).

A second `init` stops with status 2, as a new CA would invalidate every client certificate the
old one signed; `--force` replaces it. Any of the four files already there are first moved to
`certs/old-<UTC timestamp>/`, so nothing is lost. A name that's neither a hostname nor an IP
address stops it with status 2 before anything is moved, and files that can't be moved or
written stop it with status 2, naming the file and the fix.

Under `tls`, `server.pem` and `server-key.pem` stay on the server; copy `ca.pem` and the token to
each client machine over a trusted channel, such as `scp`. Then connect by a name the certificate
gives, trusting the CA (see **Connecting** below):

```sh
orbit chat --connect wss://orbit.example.lan:7433/ --ca ~/.config/orbit/tls/work-ca.pem
```

To renew the server certificate, before it expires (`orbit certs list` shows when) or to change
its names, run `orbit certs server` with the names (none for the machine's hostname, as for
`init`):

```sh
orbit certs server orbit.example.lan 192.168.1.20
```

It moves `server.pem` and `server-key.pem` to `certs/old-<UTC timestamp>/` and writes new ones
the CA signs, holding the names, `localhost` and `127.0.0.1`; `--key-type` and `--days` work as
for `init`. `orbit serve` uses them once restarted. The CA stays, so clients need nothing new.
Without `ca.pem` it stops with status 2, pointing to `orbit certs init`, and without the CA's key
it stops with status 2, naming `--ca-key` (see **Keeping the CA's key offline** below).

**Client certificates.** With `tls = "mtls"` (`--tls mtls --client-ca <path>`), `orbit serve`
needs `client_ca` too, the CA that signs client certificates. The TLS handshake asks for a
certificate and refuses one that CA didn't sign before any WebSocket opens; a client that shows
none is let through the handshake, for the health checks, but turned away from the WebSocket,
and every other path, with HTTP 401. Either way `orbit serve` prints a line saying why. The
certificate is the client's identity: the token isn't asked for, and any sent is ignored, so
`orbit serve` neither reads nor makes the token file, and its directory's permissions don't matter. Each
connect line names the certificate's subject and SHA-256 fingerprint, such as `orbit-chat
connected from 192.168.1.30:51234 as CN=laptop (3F:A2:…:9C)`.

`orbit certs client` issues a certificate for each client machine, signed by the CA `orbit certs
init` made, which `init`'s lines name as `client_ca` too:

```sh
orbit certs client laptop
```

It writes `clients/laptop.pem` and `clients/laptop-key.pem` in `certs/`, the key readable by you
alone: a certificate named `CN=laptop`, for client authentication, valid for a year or `--days`,
with `--key-type` as for `init`. It prints their paths and the certificate's SHA-256
fingerprint, as connect lines show it and `revoked` takes it. As the name names the files, it's
up to 64 letters, digits, dots, dashes and underscores, starting with a letter or digit; another
stops it with status 2. Bundle them for the client machine with `--bundle`, as below, or copy the
pair and `ca.pem` there over a trusted channel, such as `scp`, for `orbit chat`'s `--cert`,
`--key` and `--ca`. A missing `ca.pem` or CA key stops it with status 2, as for `orbit certs
server`.

A name that already has a certificate stops it with status 2, as a new one would leave the old
working. `--force` issues a new one anyway, moving the old pair to `certs/old-<UTC timestamp>/`
and printing the old certificate's fingerprint, to add to `revoked` (see below) to stop it
working:

```sh
orbit certs client laptop --force
```

`orbit chat` with `--cert` and `--key` shows the certificate rather than the token, so they
don't go with `--token-file`; one without the other, a key others can read (`chmod 600`), or
files that can't be read or don't match stop it with status 2.

**Bundles.** `--bundle <file>` writes, besides the pair, one tar holding all the client machine
needs, readable by you alone: `ca.pem`, `laptop.pem`, `laptop-key.pem` (readable by its owner
alone in the tar too) and `remote.toml`, the lines that name the server in the client's
`config.toml`, with the files where they're unpacked, in `~/.config/orbit/certs/laptop/`:

```sh
orbit certs client laptop --bundle laptop.tar
```

```toml
# Add these lines to ~/.config/orbit/config.toml, then connect with orbit chat --remote laptop.
[remotes.laptop]
url = "wss://orbit.example.lan:7433"
ca = "~/.config/orbit/certs/laptop/ca.pem"
cert = "~/.config/orbit/certs/laptop/laptop.pem"
key = "~/.config/orbit/certs/laptop/laptop-key.pem"
```

`url` is `wss://` with the server certificate's first name (the first name given to `init` or
`server`) and `port` in `[server]` (7433 unless set), or the URL `--url` gives, such as `--url
wss://orbit.example.com:9000` for a proxy in front. These stop it with status 2 before anything
is issued: no `server.pem` without `--url` (naming `--url`), `--url` without `--bundle`, an
invalid `config.toml`, and a file already at the bundle's path, which `--force` replaces
(issuing a new certificate for a client that has one, as above). A bundle that can't be written
stops it with status 2 once the pair is written, naming the file. Copy the bundle to the client
machine over a trusted channel, unpack it into the directory its `remote.toml` names, keeping the
files' permissions (`tar -p`), add its lines to the client's `config.toml`, and connect by the
remote's name:

```sh
scp laptop.tar laptop.lan:                      # on the server, to the laptop
# On the laptop.
mkdir -p -m 700 ~/.config/orbit/certs/laptop
tar -xpf laptop.tar -C ~/.config/orbit/certs/laptop && rm laptop.tar
cat ~/.config/orbit/certs/laptop/remote.toml >> ~/.config/orbit/config.toml
orbit chat --remote laptop
```

The bundle holds the client's key, so remove it from both machines once it's unpacked.

`orbit certs list` shows each certificate in `certs/`, one per line, leaving out those moved to
`old-<UTC timestamp>/`: its kind (`CA`, `server` or `client`), its name (its CN, or a client's
file name), the names a server certificate holds, the date it expires and the days left, and its
SHA-256 fingerprint. It marks one past its expiry date `expired`, one expiring within 14 days
`expires soon` (renew it before then), one whose validity hasn't started `not yet valid`, and a
client certificate whose fingerprint is in `revoked` (see below) `revoked`:

```text
CA      orbit CA (box)                   2036-10-22  3650 days left              E4:53:…:1C
server  box (box, localhost, 127.0.0.1)  2027-10-25  365 days left               CF:75:…:B1
client  laptop                           2027-10-25  365 days left, revoked      FE:01:…:DE
client  phone                            2026-11-04  10 days left, expires soon  F7:4D:…:89
```

Without `certs/` it says there are no certificates, pointing to `orbit certs init`. A file there
that isn't a certificate in PEM, or an invalid `config.toml`, stops it with status 2.

To revoke a lost machine's certificate, find its fingerprint on its line of `orbit certs list`
(or its connect line, or what `orbit certs client` printed) and add it (in either case, with or
without colons) to `revoked` in `[server]`; `orbit certs list` then marks it `revoked`. Each
entry must be a full SHA-256 fingerprint, all 64 hex digits, as the connect line or `openssl x509
-noout -fingerprint -sha256` shows it; one cut short, with a stray character or from SHA-1 could
never match, so it's refused as invalid, naming the entry:

```toml
[server]
revoked = ["3F:A2:…:9C"]
```

`orbit serve` reads `revoked` afresh for each new connection, so there's no restart: the next
connection with that certificate is refused at the upgrade with HTTP 403, naming its fingerprint,
while one already open stays open until it disconnects (restart `orbit serve` to drop it). While
the config file is invalid, it warns and keeps the fingerprints it read last.

**Keeping the CA's key offline.** `ca-key.pem` signs the certificates the server and its clients
trust, so whoever reads it can make their own. `orbit certs` leaves it in `certs/`, so the
server's account can renew the server certificate and issue client certificates unattended; to
keep it off the server, move it, once `init` is done, to a drive kept offline or another machine:

```sh
mv ~/.config/orbit/certs/ca-key.pem /media/offline/orbit-ca-key.pem
```

`orbit serve` never reads it (`client_ca` is `ca.pem`, which stays). Then `orbit certs server`
and `orbit certs client` take `--ca-key` with where it is, with the drive mounted; without it they
stop with status 2, saying the CA's key isn't in `certs/` and naming `--ca-key`:

```sh
orbit certs client laptop --ca-key /media/offline/orbit-ca-key.pem
```

A key that isn't the CA's in `ca.pem`, or isn't an Ed25519 or ECDSA key in PEM without a
passphrase, stops them with status 2 too.

**Certificates with openssl.** Or make the CA and the server's certificate with openssl, naming
the server's hostname (here `orbit.example.lan`, and its address) as a subject alternative name,
which clients check. The CA must say it signs certificates (`keyUsage`), or clients refuse the
server's certificate. With Ed25519 keys:

```sh
mkdir -p ~/.config/orbit/tls && cd ~/.config/orbit/tls
# The CA: keep ca-key.pem secret, ideally off the server; ca.pem goes to every client.
openssl req -x509 -newkey ed25519 -nodes -days 3650 -subj "/CN=orbit CA" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout ca-key.pem -out ca.pem
# The server's key and certificate, signed by the CA.
openssl req -newkey ed25519 -nodes -subj "/CN=orbit.example.lan" \
  -keyout server-key.pem -out server.csr
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -days 365 -out server.pem \
  -extfile <(printf "subjectAltName=DNS:orbit.example.lan,IP:192.168.1.20\nextendedKeyUsage=serverAuth")
chmod 600 ca-key.pem server-key.pem
cd -
orbit serve --tls tls --cert ~/.config/orbit/tls/server.pem \
  --key ~/.config/orbit/tls/server-key.pem --host 0.0.0.0
```

With ECDSA P-256 keys, for a browser or load balancer in front that needs them, each `openssl req`
takes `-newkey ec -pkeyopt ec_paramgen_curve:P-256` in place of `-newkey ed25519`:

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -subj "/CN=orbit CA" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca-key.pem -out ca.pem
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=orbit.example.lan" \
  -keyout server-key.pem -out server.csr
```

To make a client certificate with openssl, signed by the CA above (which may serve as
`client_ca` too), make its key on the client and sign its request on the machine holding
`ca-key.pem` (for P-256, `-newkey ec -pkeyopt ec_paramgen_curve:P-256` again):

```sh
# On the client: its key, which never leaves it, and a request to sign.
mkdir -p ~/.config/orbit/tls && cd ~/.config/orbit/tls
openssl req -newkey ed25519 -nodes -subj "/CN=laptop" -keyout laptop-key.pem -out laptop.csr
chmod 600 laptop-key.pem
# On the CA's machine, given laptop.csr: the certificate, sent back to the client.
openssl x509 -req -in laptop.csr -CA ca.pem -CAkey ca-key.pem -days 365 -out laptop.pem \
  -extfile <(printf "extendedKeyUsage=clientAuth")
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
| The server certificate has expired. | Renew it, with `orbit certs server` or by replacing the file, and restart `orbit serve`. |
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
| The token file can't be read. | Check the path given to `--token-file`, or the remote's `token_file`, and that it holds the token `orbit serve` uses. |
| The token file is empty. | Copy into it the token `orbit serve` uses, or name another with `--token-file`. |
| The control plane refused the token. | Give it the token `orbit serve` uses: copy it to the file named, or name another with `--token-file`. |
| The orbit serve running here refused the token. | Give it the token `orbit serve` uses, or pass `--local` to run one here. |
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
workspace picker (see [Workspaces](#workspaces)), then the session picker, which offers to resume
the session the workspace has active (see [Sessions](#sessions)), rather than running a control
plane of its own. It takes a `wss://` URL, to a server serving
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
So does `--state-dir`, which says where a control plane run here keeps its state, and so finds
one running here, neither of which a chat joining by URL does.
`--new` and `--name` skip the session picker and open a new session in the workspace picked, as
picking `New session` does. Any
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
rejected or revoked, each shown with its message from the table above. A server closing the
connection before answering is taken as speaking TLS, or as refusing the client certificate, only
until the chat first connects; after that it's retried, as a server restarting, or a proxy whose
backend is down, closes connections that way too. Likewise "Disconnected", giving the reason, shows when the connection closes in a way
reconnecting would only repeat: a message over the size limit, sent either way, the server
refusing the chat's `Hello` as incompatible (a chat and a serve speaking different protocol
versions, as after upgrading one of them), or an event from the server the chat can't read.

**Finding the server.** Without `--connect`, `orbit chat` looks for the `orbit serve` running on
its state directory, from the run file there, and connects to it as `--connect` does, the
workspace picker highlighting the workspace of the repository it runs in, or offering to add it,
printing nothing extra: the backend flags stop
it with status 2, saying they belong on `orbit serve` or call for `--local`, and `--new` and
`--name` open a new session in the workspace picked. A run file whose process is gone, or a server that
can't be reached at all, is ignored, and the chat runs in-process as usual. A server found is never
bypassed: if it refuses the token, or the token file can't be read, the chat stops with status 2,
naming the token file, and if it refuses the handshake, such as a revoked client certificate or
one its client CA didn't sign, or a client certificate it requires and wasn't shown, the chat
stops with status 2 saying what's wrong, as listed in **TLS and connection errors** above, and that
`--local` runs one here instead. `--ca` given replaces the CA the run file records, and
`--token-file`, or `--cert` and `--key`, the token or the local client certificate, as they do
with `--remote`. Only servers of the same account on the same machine are found this way; for
any other, give `--connect` its URL, or name it in `[remotes]`. `--local` skips the search and
always runs in-process, taking the backend flags; it can't go with `--connect` or `--remote`.
A chat running in-process, with `--local` or with no server found, has nothing to show
`--token-file`, `--ca`, `--cert` or `--key`, so they stop it with status 2 rather than being
ignored. `--state-dir`, which it finds a server by, and the backend flags work as ever.

A server serving TLS is found too. Its run file records the CA to trust, `ca` in `[server]`, or
`client_ca` without one, and the hostname to connect by: one its certificate gives as a subject
alternative name, `localhost` first for a server listening on every address, then `127.0.0.1` for
`0.0.0.0`, or `::1` and then `127.0.0.1` for `::`; the host itself first for one on loopback,
then `localhost`; and the host itself otherwise. With no CA set, or a certificate that names none
of these, `orbit serve` starts anyway with a warning that local discovery won't work, saying why,
and chats here run in-process unless given `--connect`. Under mTLS the chat shows the client
certificate `local_client_cert` and `local_client_key` in `[server]` name, from the same
`config.toml`, rather than the token: make one with the client CA as in **Client certificates**
above, or the one `--cert` and `--key` give. Without either it stops with status 2, naming the two
keys, rather than running in-process;
a key others can read stops it with status 2 and the `chmod 600` that fixes it, as do files that
can't be read. A server found whose TLS handshake fails in a way that would fail again stops it with
status 2 too, saying why, rather than running in-process: the recorded CA didn't sign its
certificate (as when `client_ca` stands in for a missing `ca`), the certificate has expired, or
under mTLS the local client certificate is revoked or wasn't signed by the client CA. Only a server
that can't be reached at all leaves the chat in-process.

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
key. `--remote` can't go with `--connect`, and the backend flags belong on `orbit serve`, and
`--state-dir` stops it, as they do with `--connect`.

**Checking on it.** `orbit status` prints what a running control plane is doing, then exits: the
server's URL and version; each workspace it serves, by label and path, with its sessions, the
active one marked `active`, each `running` or `idle`, or `waiting for a coder: 2nd in line`
while its run waits for a coder's slot, and `run paused: usage limit; starts again at 18:01`
while its run is paused; the agents running, by workspace, role and task; and what turns have
cost in all, today and over the lifetime, in tokens and dollars:

```
orbit serve 0.1.0 at ws://127.0.0.1:7433/

Workspaces
  orbit  /home/me/code/orbit
    Oct 25 09:12 · active · running
    docs · idle · run paused: usage limit; starts again at 18:01

Agents
  orbit · coder · task 3

Usage
  today     1,204,880 tokens · $0.84
  lifetime  15,301,112 tokens · $12.10
```

It asks the `orbit serve` running here, found as `orbit chat` finds it (see **Finding the
server** above), or the one `--connect <url>` or `--remote <name>` names, showing it the token or
client certificate `orbit chat` would, with the same `--token-file`, `--ca`, `--cert`, `--key`,
`--state-dir` and `--config-dir` (`--state-dir` can't go with `--connect` or `--remote`). It
connects as a frontend, says `Hello` as `orbit-status`, watches the control-plane stream (see
**Watching what runs** above), prints what it's told and disconnects. `--json` prints the same as
one JSON object instead: `url`, `server_version`, `workspaces` (each with `id`, `label`, `path`
and `sessions`, each with `id`, `name`, `active`, `state`, `waiting_for_coder`,
`coder_position`, `paused_until` and `paused_for`), `agents` (each with `workspace`,
`workspace_label`, `session`, `agent_id`, `role` and `task_id`) and `usage` (`today` and
`lifetime`, each with `tokens` and `cost_usd`). It exits with status 0 once it has printed; 1
when no `orbit serve` is running here, saying `No orbit serve is running for this account.`; and
2 when the control plane can't be asked, saying why: a connection, TLS or credential error, with
its message from **TLS and connection errors** above (a token refused says `The control plane
refused the token.`), a server that can't be reached, is shutting down, closes the connection or
refuses its `Hello`, or no answer within 10 seconds.

**Two machines.** To run the control plane on a server, `orbit.example.lan` (192.168.1.20), and
chat from a laptop under mutual TLS: on the server, make the CA, the server's certificate and the
laptop's bundle with `orbit certs` (see **Certificates** and **Bundles** above):

```sh
# On the server.
orbit certs init orbit.example.lan 192.168.1.20
orbit certs client laptop --bundle laptop.tar
```

put the `[server]` lines `init` printed in its `config.toml`, listening beyond loopback:

```toml
# The server's ~/.config/orbit/config.toml
[server]
host = "0.0.0.0"
tls = "mtls"
cert = "/home/me/.config/orbit/certs/server.pem"
key = "/home/me/.config/orbit/certs/server-key.pem"
client_ca = "/home/me/.config/orbit/certs/ca.pem"
```

and run `orbit serve`. Then fetch the bundle to the laptop over a trusted channel, unpack it,
keeping its permissions, add its remote to the laptop's `config.toml` and connect:

```sh
# On the laptop.
scp orbit.example.lan:laptop.tar . && ssh orbit.example.lan rm laptop.tar
mkdir -p -m 700 ~/.config/orbit/certs/laptop
tar -xpf laptop.tar -C ~/.config/orbit/certs/laptop && rm laptop.tar
cat ~/.config/orbit/certs/laptop/remote.toml >> ~/.config/orbit/config.toml
orbit chat --remote laptop
```

The remote is named `laptop`, for the certificate; rename its table, `[remotes.laptop]`, in the
laptop's `config.toml` to connect by another name, such as `orbit chat --remote work`.

Under `tls` instead (`tls = "tls"`, with `ca` in place of `client_ca`), there are no client
certificates or bundles: `orbit serve` makes the token on first start, and you copy `ca.pem` and
the token to the laptop over a trusted channel, keeping both readable by you alone, and name them
in a remote:

```sh
# On the laptop.
mkdir -p ~/.config/orbit/tls && chmod 700 ~/.config/orbit
scp orbit.example.lan:.config/orbit/certs/ca.pem ~/.config/orbit/tls/work-ca.pem
scp orbit.example.lan:.config/orbit/token ~/.config/orbit/work-token
chmod 600 ~/.config/orbit/work-token
```

```toml
# The laptop's ~/.config/orbit/config.toml
[remotes.work]
url = "wss://orbit.example.lan:7433/"
ca = "tls/work-ca.pem"
token_file = "work-token"
```

and connect with `orbit chat --remote work`. In all, the server keeps `ca.pem`, `server.pem` and
`server-key.pem`, and the token under `tls` (`ca-key.pem` is best moved off it once the laptop's
certificate is made, see **Keeping the CA's key offline** above); the laptop keeps its copy of
`ca.pem`, with `laptop.pem` and `laptop-key.pem` under `mtls`, or the token's copy under `tls`.

### Hosting in a container

The recommended way to host the control plane is in a container, built from this repository's
own files: `deploy/Dockerfile` makes an image of `orbit serve` (Python 3.14, git, Node.js LTS and
the Claude Code CLI pinned by the `CLAUDE_CODE_VERSION` build argument, and orbit installed from
the checkout with `uv sync --frozen --no-dev`), and `deploy/compose.yaml` runs it over TLS. The
image holds no credential: Claude's login, the token, the certificates and their keys live in
volumes, and `.dockerignore` keeps `.env`, `orbit-config`, keys and `.git` out of the build.

The compose file runs one service, `orbit`, as your user (`user: "${UID:-1000}:${GID:-1000}"`,
so the commits it makes in your checkout are yours), with these mounts:

| Mount | In the container | What |
| --- | --- | --- |
| the repository (`..`) | `/workspace` | the checkout `orbit serve --add /workspace` serves; git trusts it through `safe.directory` |
| `deploy/orbit-config` | `/home/orbit/.config/orbit` (`ORBIT_CONFIG_DIR`) | `config.toml`, the token and the certificates |
| the volume `orbit-state` | `/home/orbit/.local/state/orbit` (`ORBIT_STATE_DIR`) | the store and run files |
| the volume `claude-login` | `/home/orbit/.claude` (`CLAUDE_CONFIG_DIR`) | Claude Code's login |
| `~/.gitconfig`, read-only | `/home/orbit/.gitconfig` | your commit identity |

The `deploy/orbit-config` and `~/.gitconfig` mounts are binds in compose's long syntax with
`create_host_path: false`, so a missing source stops `docker compose up` with an error, rather
than Docker making it an empty root-owned directory on the host (which `orbit serve` can't write
its token to, and which at `~/.gitconfig` breaks git on the host too). So make
`deploy/orbit-config` before you start (step 1 below), and have a file at `~/.gitconfig`. If your
identity lives elsewhere, such as in `~/.config/git/config`, either make `~/.gitconfig` or point
the gitconfig mount's `source:` at another file, such as one holding just your identity:

```ini
[user]
	name = Your Name
	email = you@example.com
```

Bash doesn't export `UID` or `GID`, so unless your ids are 1000, run `export UID; export GID=$(id
-g)` first, or put both in `deploy/.env`; the image is built with the same ids.

To set it up, from the repository's top level, with no orbit work left from the host (see
**Switching between host and container** below):

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

**Switching between host and container.** Git records a worktree by its absolute path, and the
container sees your checkout at `/workspace`, not where it lives on the host, such as
`/home/me/code/orbit`. So the worktrees an `orbit chat` or `orbit serve` on the host made under
`.git/orbit` point at host paths the container doesn't have, and those the container makes point
at `/workspace`, which the host doesn't have: on the other side, git commands in them fail, and
a `git worktree prune` there drops their records as stale. Before you switch, either way, finish
or clean up orbit's work: no tasks running and nothing left in `.git/orbit`. And while the
container is working, don't run `git worktree prune` on the host, which would delete the records
of the container's live worktrees.

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
first line says so too, for each workspace it's read-only in, and the workspace's entry in its
catalog is marked read-only. Running
tasks (`ctrl+r`) and the triggers (`ctrl+g`, `ctrl+e`) are accepted and queued, in order, but
nothing runs: no task, review or startup check. Those an owning orbit would refuse, such as a super
review with reviews off, are refused just the same. It notes no work left in `.git/orbit` by
earlier runs, since what's there may be the owner's running work. The planner, the sessions, drafts
and settings work as usual. The queue is dropped when the orbit exits, and when you switch
sessions, with a note saying how many queued commands were dropped, so you ask for them again in
the session you want them run in.

A read-only orbit tries the lock every 5 seconds. Once the owner has gone, it takes the lock and
asks, once, in a modal: "The repository is free". `y` takes it over: the orbit owns the
repository from then on, the "read-only" mark goes, `orbit serve` names its URL in the lock, a
note says "This orbit now owns the repository; queued work is running." and the work queued runs, in order. `n` or `esc` stays
read-only: the orbit lets the lock go again, so another orbit can take it, and asks again once
another orbit has owned the repository and gone, or after 5 minutes if it's still free. Under `orbit serve`, every frontend connected asks, as does
one that connects while the question is open; once one answers, the others close the modal. An
orbit that owns the repository never asks.

### Telemetry

Orbit can export what it does as OpenTelemetry spans and metrics, to a collector over OTLP, to
stderr, or to files. It's off unless the user config turns it on, and then needs an extra that installs the
OpenTelemetry SDK: `orbit[telemetry]` for OTLP over HTTP, the console and the files, or
`orbit[telemetry-grpc]` for OTLP over gRPC.

```sh
uv tool install '/path/to/orbit[telemetry]'    # or: pip install 'orbit[telemetry]'
```

Telemetry configured without its extra stops `orbit chat` and `orbit serve` with status 2:
`Telemetry is configured but not installed: pip install 'orbit[telemetry]'`. With it off, orbit
imports nothing of the SDK. `[telemetry]` in the user config sets it up:

```toml
[telemetry]
exporter = "otlp"                 # "none" (the default), "otlp", "console" or "file"
endpoint = "http://localhost:4318"  # the collector's base URL; the exporter's default if unset
protocol = "http/protobuf"        # or "grpc"
headers_file = "otel-headers"     # headers sent to the collector; chmod 600
file_max_mb = 50                  # the file exporter's files grow to this size each
file_keep = 5                     # and this many are kept
metrics_interval_s = 60           # metrics are exported this often, in seconds
service_name = "orbit"            # the service.name spans and metrics are sent under
```

Over HTTP, orbit adds `/v1/traces` to `endpoint` for spans and `/v1/metrics` for metrics; over
gRPC it's used as it is, such as `collector:4317`. `headers_file` is taken from the config directory if relative, with `~`
expanded, and holds one `Name: value` per line, such as `Authorization: Bearer …`; blank lines and
lines starting with `#` are ignored. As it holds secrets, it must be readable by you alone: one
others can read stops orbit with status 2 and `run chmod 600 <path>`. The standard variables
override the configuration: `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` for spans and
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` for metrics, each used as it is, or else
`OTEL_EXPORTER_OTLP_ENDPOINT`, a base URL, for `endpoint`; and `OTEL_EXPORTER_OTLP_HEADERS` and
then `OTEL_EXPORTER_OTLP_TRACES_HEADERS` for spans or `OTEL_EXPORTER_OTLP_METRICS_HEADERS` for
metrics (`name=value` pairs separated by commas) over the headers file's, header by header. An unknown key in `[telemetry]`, or a value it doesn't take, stops orbit
with status 2 and a message naming the file and the key.

`"console"` writes each span, and the metrics each `metrics_interval_s`, to stderr as JSON, and is
for `orbit serve` alone: as the TUI uses the terminal, `orbit chat`, running its own control plane
or joining one, stops with status 2 before the TUI starts, suggesting `"file"` or `"otlp"`
instead. `"file"` writes OTLP JSON, as the Collector's `otlpjsonfile` receiver reads it, to the
state directory, readable by you alone: spans, one `ExportTraceServiceRequest` per line, to
`telemetry/spans-*.jsonl`, and metrics, one `ExportMetricsServiceRequest` per line, to
`telemetry/metrics-*.jsonl`. Each starts a new file before one would grow past `file_max_mb`,
deleting its oldest so `file_keep` of each remain.

Every span is kept: there's no sampling. Spans describe the orbit that made them with
`service.name`, `service.version`, `service.namespace` (`orbit-serve`, or `orbit-chat` for a chat,
running its own control plane or joining one), `service.instance.id`, new each start, and
`host.name`, as do metrics. Spans are
batched and sent in the background, and metrics every `metrics_interval_s`; when `orbit chat`
closes, or `orbit serve` stops gracefully, orbit waits at most 5 seconds for the spans and metrics
not yet sent, then logs one warning naming how many spans it dropped, such as `Telemetry dropped 3 spans it couldn't export.` `orbit serve` logs warnings, from
orbit and OpenTelemetry alike (an unreachable collector, say, or a span file that can't be
written), to stderr. `orbit chat` keeps them
off the screen while the TUI runs, showing each kind (the same logger and message) once per run as
a transcript note (`⚠ Telemetry: …` for telemetry's), and prints every one to stderr, in order,
after it closes, followed by the dropped spans' warning. Orbit's unexpected errors are printed
after it closes with their tracebacks; those the conversation already shows aren't noted again,
while the rest, such as a failure flagging a workspace served or not as orbit cleans up after
another, are noted like any warning.

Each task the work lane runs is one span, `orbit.task`, starting its own trace: who the task is
(workspace, session, id, title, role, model and who queued it), what its turns cost in tokens and
dollars, how long it waited for a coder's slot, its fix attempts and the checks its agent ran,
how verification and merging came out, and its final status and reason, with each verification
step, a give-up and a merge conflict as events, and its wait and run as child spans. Each command
a frontend sends is one span, `orbit.command`: its type, the workspace and session it's for, the
client that sent it and whether it was accepted or refused, with the code; it's a child of the
span whose trace context its envelope carries, if any, else it starts its own trace, and the
events it publishes carry its own. Each planner turn is one span, `orbit.planner.turn`, a child of
the `SendMessage` command's: the workspace, session and model, what the turn cost, the drafts it
proposed and how it ended. Each review, a reviewer's or a super reviewer's, is one span,
`orbit.review`, starting its own trace: the commits, round and batch it reviewed, its cap, the
suggestions it made and had refused, and what it cost. Work that runs on in the background links
to what started it rather than nesting under it: each task's span links to the `RunTasks` (or,
for a configurator, `FireTrigger`) command that started its run, or to the review whose
suggestions it runs, and keeps that link when a run the usage limit or an API outage stopped
starts again; each review's links to the task whose landing made it due, or, for a super review,
to the
`FireTrigger` command or the landing that called for it. Each agent process is one span,
`orbit.agent.process`, under the task's run, the planner's turn or the review it works for: its
role, how it exited, its turns and the tools it called, as sorted names with their counts; each
call it makes to orbit's tools is a child span, `orbit.agent.request`, with the tool and whether
the reply was an error, and each verification step a call runs, verifying a `complete_task` claim
or for `run_check`, is a child of that call, `orbit.verify.step`, with its name and result. Each
run of the startup check is one span, `orbit.startup_check`, starting its own trace: the
workspace, how it came out (`passed`, `failed` or `cancelled`), the step that failed and whether
it added a draft task to fix it, with each step it ran a child `orbit.verify.step`. Each
frontend's WebSocket to `orbit serve` is one span, `orbit.connection`, starting its own trace,
from when it's accepted until it's closed: where it connected from, the client and protocol
version its `Hello` gave, and, as events, that `hello`, a `reconnect` with the `seq` it catches up
from and how many were stamped since, each `attach` (with the workspace and session) and
`detach`, and the `close`, with its code and reason. Spans hold no prompts, agent messages, tool
arguments or replies, diffs or check output. [docs/telemetry.md](docs/telemetry.md) lists every span, attribute, event
and link.

Metrics come from the spans, recorded as each ends, for backends that can't derive them: how many
tasks ran, by final status, role and workspace (`orbit.tasks`); how long verification steps
(`orbit.verify.step.duration`, by step and result) and agent processes (`orbit.agent.duration`, by
role and runtime) ran; the tokens tasks, planner turns and reviews used, by model, kind of token
and role (`gen_ai.client.token.usage`), and what they cost (`orbit.cost`); and the suggestions
reviews made and had refused (`orbit.review.suggestions`). They go where the spans go, every
`metrics_interval_s` seconds, 60 unless set; a value that isn't a whole number of seconds, at
least 1, stops orbit with status 2 naming the key. With `exporter = "none"`, orbit records none.
Live gauges, read from the control plane as the metrics are exported, go with them: the agents
running, by role and workspace (`orbit.agents.active`), the tasks waiting for a coder's slot
(`orbit.queue.depth`), the pending tasks of each workspace's open session (`orbit.tasks.pending`),
the frontends connected (`orbit.frontends.connected`) and the bytes each workspace's sessions' events
take in the store (`orbit.event_log.size`). [docs/telemetry.md](docs/telemetry.md) lists every
metric, with its kind, unit, attributes and source.

With `exporter = "otlp"`, the agents orbit runs send the Claude Code CLI's own telemetry, its
metrics and logs, to the same collector, so they land beside orbit's. Each agent's environment
gets `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp`,
`OTEL_EXPORTER_OTLP_PROTOCOL` (the `protocol`), `OTEL_EXPORTER_OTLP_ENDPOINT` (the collector's base
URL: `OTEL_EXPORTER_OTLP_ENDPOINT` in orbit's environment, else `endpoint`, else OTLP's default,
`http://localhost:4318`, or `http://localhost:4317` over gRPC) and `OTEL_RESOURCE_ATTRIBUTES`,
labelling what it sends with `orbit.task.id` (the planner, having no task, has none),
`orbit.agent.id`, `orbit.role` and `orbit.workspace.id`. The headers are never passed, from the
headers file or the environment: an agent can run any command, so it could leak the collector's
key. Agents don't inherit `OTEL_EXPORTER_OTLP_HEADERS`, nor the traces', metrics' or logs' own
headers variables, so a collector that needs headers won't receive agents' telemetry. With
`"console"`, `"file"` or `"none"`, orbit passes agents none of these variables.

The TUI traces what the user does: each action that sends commands, such as pressing ctrl+r or
picking a session, is one span, `orbit.tui.action`, with `orbit.action` naming it (`run_tasks`,
`submit_prompt`, `pick_session` and so on), and every command it sends carries its trace context,
so their `orbit.command` spans are its children and one trace reads from the key pressed to what
its commands did. An action sending several commands, such as picking a session (`Attach`, then
`ActivateSession`), is one span; one sending nothing, such as hiding the sidebar, has none, and
what the TUI sends on its own, such as its `Hello`, carries no trace context. A chat running its
own control plane traces its TUI with the control plane's telemetry. A chat joining one
(`--connect`, `--remote`, or an `orbit serve` found running here) starts its own from the
`[telemetry]` in the user config where it runs, the frontend user's, as the control plane's
collector may be out of its reach, and flushes and stops it as it closes; without one, its
commands carry no trace context, and their `orbit.command` spans start traces of their own.

Traces connect from the key the user pressed onwards. An `orbit.tui.action` span is the parent
of the `orbit.command` spans of the commands it sent. A `SendMessage` command's span is the
parent of the `orbit.planner.turn` it asked for, which is the parent of the planner's
`orbit.agent.process`. Each task of the run a `RunTasks` command started is a trace of its own,
its `orbit.task` span linking to that command's; the task's run is the parent of the coder's
`orbit.agent.process`, which is the parent of the agent's `orbit.agent.request`s to orbit, each
the parent of the `orbit.verify.step`s it ran. A review a task's landing made due is a trace of its
own too, its `orbit.review` span linking to that task's and the parent of the reviewer's process;
the tasks running its suggestions link to the review in turn. So a task can be followed back to
the command that started its run, and that command to what the user did in the TUI. The startup
check and each connection are traces of their own, linked to nothing, as no command asks for
either.

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
  message limit, `bearer_header` and its reader `bearer_token` (in `transport`), which the
  control plane, the MCP host and `orbit.client` share. It imports no other orbit package. `docs/protocol.md` describes the protocol, its conversation rules and
  codes, and `schema/protocol.schema.json` is its JSON Schema.
- `orbit.client`: the frontends' side of a control plane in another process. `WebSocketSession`
  (in `session`) is a `Session` over a WebSocket (the `websockets` library) that reconnects with
  backoff, but not after a close that would recur (1000 or 1009), sends its last `Hello` again
  with `since`, and reports how the link stands through
  `orbit.protocol`'s `Link`; `read_token` (in `token`) reads the token it shows, and `accepts`
  (in `probe`) asks a control plane whether it takes it; both open the WebSocket through
  `open_connection` (in `upgrade`), which raises `TokenRejected` on a 401. It imports no
  orbit package but `orbit.protocol`, and only `orbit.cli` imports it.
- `orbit.tracing`: the tracing port, standard library only. `Telemetry` starts `Span`s, each a
  child of the current span, which a `ContextVar` keeps (`current_span`), so it follows the
  code across awaits and into the tasks started under it, and linked to any others it's given;
  it carries the current span's trace context in an envelope (`carry`) and finds the remote
  span one carries (`carried`), knowing envelopes only as `Carrier`s. `NoTelemetry` records
  and carries nothing but still nests spans. `Gauges` reports live values, each gauge a
  callback read as metrics are collected; `NoGauges` reports nothing. It imports no orbit package, and neither
  `orbit.protocol` nor the backend's domain imports it. Trace context crosses the wire in the
  envelopes' `traceparent` and `tracestate`, which `orbit.protocol`'s `trace_carrier` and
  `with_trace_carrier` read and set.
- `orbit.telemetry`: the tracing port over the OpenTelemetry SDK, the only package that imports
  `opentelemetry`, and only `orbit.cli` imports it. `TelemetrySettings` is the user config's
  `[telemetry]`; `start_telemetry` gives `Off`, importing nothing of OpenTelemetry, or an
  `OtelTelemetry` (in `otel`), whose spans nest under `orbit.tracing`'s current span and are
  batched to an OTLP, console or `OtlpJsonFileExporter` (in `file_exporter`) exporter, whose
  `MetricsFromSpans` (in `metrics`) records metrics from those spans as they end, exported every
  `metrics_interval_s` by the matching OTLP, console or `OtlpJsonMetricsFileExporter` exporter
  with the observable gauges its `OtelGauges` (in `gauges`) registers, and which carries trace
  context into and out of envelopes with the W3C propagator; each gives the CLI the `Gauges` it
  hands the control plane. It knows neither the
  backend nor the frontends.
- `orbit.agent`: backend, with no UI imports. Three layers, each importing only the ones below:
  - `external`: adapters that reach outside the backend. `ClaudeCliRunner` launches agents as
    `claude -p --output-format stream-json` runs connected to orbit (`run_agent`, and `converse`
    for the planner, which resumes its Claude session turn after turn); `McpHost` is orbit's
    MCP server; `GitCoderTree` is the coder's worktree and its snapshots; `repository` finds the
    git repository a workspace is in, fingerprints it, and places orbit's directory in it
    (`orbit_dir`: `.git/orbit`, or `.orbit` outside git); `config_file` reads
    `.orbit/config.toml`, and for `orbit config show` what the configuration is resolved from
    (`read_config_sources`), and `role_instructions` the instructions files its roles and your
    personal roles name (`resolve_instructions`);
    `built_in_roles` reads the built-in roles from `instructions/roles.toml` and their
    instructions from the Markdown files it names (`read_instruction_files` names them); `checks`
    runs a verification step as a process (`run_step`); `processes` runs a program to completion
    without leaving it behind; `workspace`
    fingerprints the git working tree; `usage_api` fetches the Claude subscription's usage
    limits (`UsageApi`) with the token in `~/.claude/.credentials.json`; `sqlite_store` is the
    `SessionStore` port's adapter (`SqliteStore`): saved sessions and each repository's super
    review count and record moves in `orbit.db` in the state directory, with `session_locks`' lock files while
    an orbit has a session open, `sqlite_schema` creating, migrating and opening the database,
    `sqlite_log` holding the events' log's statements, `sqlite_backup` copying it to a backup and `sqlite_errors` telling its errors apart;
    `directories` picks the state and config directories (`resolve_state_dir`,
    `resolve_config_dir`); `session_import` imports the session files
    older orbits saved in orbit's directory, under the names `session_files` gives. `WorkspaceLock`
    (in `workspace_lock`) is the lock the orbit owning a repository holds.
    `ControlPlaneServer` (in `control_plane`) is the server adapter: it serves `ControlPlane`
    over a WebSocket (starlette on uvicorn, through `embedded_server`'s `EmbeddedServer`, which
    leaves signals to the program), one `SessionConnection` per frontend that shows the token,
    turning frames into commands and events into frames, and traces each WebSocket it accepts
    in an `orbit.connection` span until it closes; `TokenFile` (in `token_file`) is the
    token it checks, read afresh for each frontend, and rotates it, `tls_warnings` (in `tls`) checks
    the server's TLS settings, key and certificate before it listens, with the messages in
    `tls_messages`, `RunFile` (in `run_file`) says where it listens, both written through
    `whole_files`, and `user_config` reads `[storage]`, `[server]`, `[remotes]` and `[roles]`.
  - `application`: `ControlPlane` (in `control_plane`) is what frontends connect to: each
    `SessionConnection` (in `connection`) from its `connect()` implements `Session`. It owns
    what's shared: the connections, the `Outbox` (in `outbox`), which stamps each event with its
    `seq`, the session it belongs to and the trace context of the span current, every stream's
    `Handshake` and the `UsageMonitor`; it routes every command to the `WorkspaceHost` of the
    workspace it's for, handling each in an `orbit.command` span (`command_span`, in
    `command_trace`) and recording a connection's `Hello`, attaching and detaching on its
    `orbit.connection` span (in `connection_trace`), and passes each host's events to the
    connections; `PlaneGauges` (in `plane_gauges`) registers its live gauges with the `Gauges`
    port; frontends add and
    remove workspaces through its
    `WorkspaceSource` port, which `orbit serve` fills and a local orbit doesn't. `WorkspaceHost` (in `workspace_host`) is one workspace's
    backend: its open session, lanes, agents and workspace lock; it takes the commands routed
    to it, asks the domain what follows, drives the ports and publishes its events. `Tasks` (in
    `tasks`) holds the `TaskBoard`, marks tasks running and finished, and is the one place that
    publishes the board, whole, once its callers have changed it. `WorkLane` (in `work_lane`) is
    the work lane: it runs the board's tasks one at a time, queues a fix after each failed step
    and lands work that passes, and `WorkTurns` (in `work_turns`) decides when it runs: when the
    user asks (`ctrl+r`), when a review queues work unless the user held it, first for a
    configurator, and again by itself once the usage limit that stopped it resets, timed by a
    `LimitPause` (in `limit_pause`), saying meanwhile until when and why it's paused, which
    `SessionStates` (in `session_state`) publishes with the session's state; each coder's task takes one of the control plane's
    `CoderSlots` (in `coder_slots`) first, and each is traced as an `orbit.task` span by
    `TaskTracing` (in `task_trace`), current while it runs so what runs it records on it, and
    linked to the span that started its run, which `WorkTurns` keeps for the run.
    `AgentRun` (in `agent_run`) sees one task through one agent, and a
    `ProposalTrial` (in `proposal_trial`) tries the checks a configurator proposes and has the
    user decide on them; `AgentDesk` (in `desk`) launches agents and routes their calls by token,
    tracing each agent's process as an `orbit.agent.process` span and each of its calls as an
    `orbit.agent.request` child of it with `AgentTracing` (in `agent_trace`);
    `Verifier` (in `verifier`) runs the steps, each run for an agent's call timed as an
    `orbit.verify.step` child of the call's span; `Lander` (in `landing`) commits and merges
    verified work; every lane of the roles' but the one whose roles merge runs on a `LaneRunner`
    (in `lane_runner`), which starts its tasks by itself, one at a time, unless a pause or a halt
    stops it, and `Lanes` (also there) holds the lanes by name; `ReviewLane` (in `review_lane`)
    queues reviews and runs them on its lane's runner, beside the work, each traced as
    an `orbit.review` span by `ReviewTracing` (in `review_trace`); `StartupCheck`
    (in `startup_check`) runs the steps once on the base commit as orbit starts, each run traced
    as an `orbit.startup_check` span by `StartupTracing` (in `startup_trace`); `UsageMonitor`
    (in `usage_monitor`) fetches the usage limits on a schedule and after an agent's turn; these
    three run their work through `Background` (in `lanes`), which reports a crash rather than
    losing it, and the work lane's and the planner's turns run through a `TurnSlot` each (also in
    `lanes`), which refuses a second turn and shows the lane busy while one runs;
    `FrontendReview` (in `proposals`) is where the user decides on proposed checks; `PlannerChat`
    (in `planner`) is the planner's conversation, running each turn and publishing how it ended;
    `PlannerSeat` (in `planner_seat`) is the planner's place in the session: it holds the current
    `PlannerChat` and its turns, starts, cancels and resets them, and closes the planners a
    switch or reset retired once that's done; each turn is traced as an `orbit.planner.turn`
    span by `PlannerTracing` (in `planner_trace`), current while it runs so the `PlannerChat`
    records on it; `SessionKeeper` (in `keeper`) is the open session:
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
    any one session; `RecordMoves` (in `record_moves`) moves a record a task names whose
    number another record took, keeping each move in the store and telling the planner; `SessionSettings` (in `settings`) holds the open session's settings, at
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
    verifying, how many fixes to try per step, and the fix prompt), `usage_limits` (whether an
    agent's error says it hit Claude's usage limit, and when a run it stopped starts again),
    `AgentTask` (in `agent_task`:
    what orbit replies to an agent, and when its work is verified), the `AgentRegistry` (in
    `agents`: agent ids and tokens), `roles` (built-in roles, loaded from their `roles.toml` and
    given their instructions, which of a role's fields go together, and the tasks orbit queues in
    them), `config` (`OrbitConfig`: the
    limits, roles, verification steps and setup a project file sets over the built-in defaults), `layers` (the layers a setting comes from, the user config's role fields and personal roles, and each role resolved across them field by field), `config_report` (what `orbit config show` reports: each setting in effect by its dotted key with the layer it came from, and a key's settings) and `config_errors` (`ConfigError`,
    and what's wrong with an invalid file or proposal), `server` (`ServerSettings`, the user config's `[server]` with its `TlsSettings`, which files each TLS mode needs, where now falls in a certificate's validity, and which hosts are loopback), `certificate_list` (what `orbit certs list` shows of each certificate: its expiry, days left, fingerprint and marks), `client_bundle` (what `orbit certs client --bundle` puts in a client's bundle: the CA, its certificate and key, and the `[remotes.<name>]` lines naming them where it's unpacked), `remotes` (`RemoteSettings`, a `[remotes.<name>]` table, and which credentials go together), `ownership` (`WorkspaceOwner`, who a workspace lock names, and the read-only note), `checks` (the verification steps, the
    setup step that makes a fresh worktree ready, and the checks a configurator proposes), the
    `Catalog` (in `catalog`: the workspace and its sessions, the agents' roles, the triggers and
    the lanes), `ReviewPlanner` (in `reviews`: when to review, batches, rounds and caps), commit
    messages (`commits`), `coder_tree` (how the coder's work is merged into the base branch, how
    its worktree catches up with that branch, and the notes on what earlier runs left) and `usage` (parsing the subscription's usage limits, and when to fetch
    them again). `sessions` holds the rules of saved sessions: the `SessionRecord` and its format
    (versioned, with upgrades from older ones, or why a file is unreadable), how the list of
    sessions summarizes and orders them, when one may be opened, renamed or deleted, and the
    notes and settings a resumed session starts with, and the note on sessions imported from an
    older orbit's files.
- `orbit.tui`: frontend. Sends commands and renders events. Imports only `orbit.protocol`, and
  `orbit.tracing` to trace the user's actions with the `Telemetry` the app is given.
  `view` holds its decisions and doesn't import Textual: `frontend` is the `Frontend` the app
  hands everything but Textual's part to (what it shows of each event, what the keys and prompts
  send, which window is selected), over `router`, which takes each event to the panes and hosts
  that show it, `commands`, the `CommandDesk` that sends commands and follows their outcomes,
  `actions`, which runs each user action as an `orbit.tui.action` span, started as it sends its
  first command, whose trace context the envelopes it sends carry (`carrying`), and `feed`, the
  `ConversationFeed` that turns events into the transcript's entries; `navigation`,
  `place`, `opening` and `catalog`, with the pickers' models (`session_picker`, `workspace_list`,
  `workspace_picker`, `explorer` and `typing_list`), are where the frontend is and how it moves;
  each window and modal has a model (`conversation`, `log`, `tab_strip`, `prompt`, `tasks`,
  `agents`, `panels`, `detail_views`, `working`, `history`, `task_editor`, `proposals`,
  `settings`, `storage`, `takeover`, `link` and `windows`); and the rest are the rules they share
  for what each piece reads as (`events`, `transcript`, `tool_calls`, `tabs`, `detail`, `diffs`,
  `diff_numbers`, `progress`, `rules`, `turns`, `sessions`, `workspaces`, `triggers`, `confirm`,
  `outcomes` and `common`). The widgets and modals show those models (`transcript`,
  `transcript_log`, `tabs`, `tasks`, `task_editor`, `reason`, `agents`, `panels`, `detail`,
  `working`, `proposals`, `sessions`, `workspaces`, `explorer`, `link`, `storage_halt`,
  `takeover` and `settings_pickers`), with `entries` for the renderables that draw an entry,
  `list_pane` for the list pane the lists share, `modal` for the slot holding a host's one modal,
  `footer` for the key footer and `widgets` for the building blocks they share; `event_stream`
  batches the backend's events so a flood still lets the screen repaint, and `app` is the Textual
  app that holds the widgets, does what only Textual does (focus, key bindings and screens) and
  hands the rest to `Frontend`.
- `orbit.cli`: the composition root, and the only place the two sides are wired together: it
  plugs the external adapters into a `WorkspaceHost`, builds a `ControlPlane` around it and
  hands that to the TUI, with the telemetry both trace with. It runs the backend on its own
  thread and event loop through
  `ThreadedSession` (in `threaded`), so neither side's work stalls the other. `orbit serve`
  plugs them in the same way but hands the `ControlPlane` to `ControlPlaneServer` instead, on the main thread's event loop. `orbit chat --connect` builds no
  backend: it hands the TUI an `orbit.client.WebSocketSession`, which runs on the TUI's own event
  loop, and telemetry of its own; neither does a chat that `discover_control_plane` finds a
  running `orbit serve` for.
  It's a module per command (`chat`, `serve`, `status`, `config` for `orbit config show`, and
  `maintenance` for `version`, `db` and `token`), with `backend` wiring the adapters for both chat
  and serve, `status_report` what `orbit status` reports and how it prints it, `config_text` how
  `orbit config show` prints the configuration, `options` the options they share and `errors` how
  they exit; its `__init__` registers the commands on the app. `orbit status` joins a control
  plane as `orbit chat --connect` does, through an `orbit.client.WebSocketSession`, on an event
  loop of its own.

These rules are enforced by `lint-imports` (see `[tool.importlinter]` in `pyproject.toml`).
