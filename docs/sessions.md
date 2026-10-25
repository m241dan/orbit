# Saved sessions

How orbit keeps the work in progress so you can come back to it, and what is deliberately left for
later. README's [Sessions](../README.md#sessions) section says how to use them.

## Goal

Quitting orbit, or a crash, shouldn't lose what you were working on, and you should be able to keep
several lines of work apart and switch between them.

## What a session holds

A session is one JSON record (`SessionRecord`, in `orbit.agent.domain.sessions`):

- its id, its name, and when it was created and last active;
- the task list, each task with its status, settings and usage;
- the review lane's state: the review jobs it queued, their batches and rounds;
- the planner's Claude conversation id, so its next prompt resumes that conversation;
- the planner's model and new tasks' tool access.

The conversation window's transcript isn't in the record: every event of a session is kept in the
store's event log instead, and a reopened session shows its last 500 events, paging back to its
start, or, once the log was trimmed, to the line "Older events were removed (session_log_mb)" (see
README's [Sessions](../README.md#sessions)). Nor does the record hold the count of commits towards
the next super review, which belongs to the repository and is kept in the store's `reviews` table,
one row per workspace, or anything from `.orbit/config.toml`, which is read at startup as before.

## Design

- **Where.** `SqliteStore` keeps them in `orbit.db` in the state directory: `--state-dir`, else
  `$ORBIT_STATE_DIR`, else `$XDG_STATE_HOME/orbit`, else `~/.local/state/orbit`. One database
  serves every repository, with each session keyed by its workspace, so the checkout never sees
  them. Sessions an older orbit saved as files are imported once, at startup (see README's
  [Where orbit keeps things](../README.md#where-orbit-keeps-things)).
- **Writes.** Each save is one SQLite transaction, so a crash leaves the old record or the new one,
  never half of either. One connection serves the database, in WAL mode, owned by a single thread
  that runs every call. Saves run one at a time, the newest last, and only when something changed.
  A failed write to the store, whether the record, the event log or the review count, halts the
  session rather than carrying on: events that couldn't be saved are parked in a fallback file
  until "Try again" (`ResumeStorage`) succeeds and appends them to the log (see README's
  [Storage failures](../README.md#storage-failures)).
- **When.** A session is saved after every change, but only once it's used: once it has a task
  or you prompt the planner. Starting orbit and quitting leaves nothing behind.
- **Format.** Each record carries a format version. Older versions are upgraded as they're read
  (version 2 added what the session spent, version 3 dropped the review planner's rounds,
  version 4 added why each task ended badly, version 5 the tasks each review reviews,
  version 6 the titles the review planner has suggested, version 7 the notes the planner is
  still to hear, and version 8 the effort in the session's and its tasks' settings, which a
  record of an older version reads as unset); a newer one, or a record that isn't
  valid JSON or doesn't fit the record, is unreadable. Records are kept as raw JSON text in their `sessions`
  row, so an unreadable one stays as it was: it is listed with the reason, can be deleted, and is
  never opened, renamed or overwritten.
- **Lifetime.** Sessions are kept until you delete one; no session is pruned, but a session's
  event log is trimmed of its oldest events once it grows past `session_log_mb` (see
  [Where orbit keeps things](../README.md#where-orbit-keeps-things)). Deleting removes the record,
  its event log and its lock, not Claude's own conversation files.
- **Locks.** An orbit locks a session from its first save until it closes or switches away, with
  an exclusive `flock` on `<state dir>/locks/<workspace id>/<session id>.lock`, so two orbits can't
  both take it. Another orbit lists the session as open elsewhere and won't open, rename or delete
  it. The kernel drops the lock when its process ends, so a crash leaves no stale lock. Renaming or
  deleting a session that isn't open takes its lock for as long as that takes.
- **Switching.** The session picker, at startup and on `ctrl+p`, opens a saved session or starts a
  new one. Switching
  is refused while tasks run, a review runs or the planner answers, so nothing is running when the
  session's contents are replaced. Opening a session never starts work. Drafts orbit added before
  you picked at startup carry over into the session you pick, and the session they were added to
  is then deleted.
- **Crash recovery.** Tasks saved as running or verifying come back cancelled, so the next run
  picks them up again, and orbit says how many. An interrupted review ends as a cancelled one
  does: if its batch is then due a review, one is queued for the next run.
- **Settings.** A session's saved model, tool access and effort come back when it opens, except
  those given on the command line with `--model` or `--tools`, which hold for the whole run.
- **Layers.** The domain module holds the rules (the record and its format, summaries, when a
  session may be opened, renamed or deleted, crash recovery) without I/O. The application's
  `SessionKeeper` decides when to save and drives switches through the `SessionStore` port, and
  `SqliteStore` in `sqlite_store` is the adapter, with `sqlite_schema` to create, migrate and
  open the database, `sqlite_log` for the events' log's statements, `sqlite_backup` to back it up, `session_locks` for the locks and
  `session_import` to import an older orbit's files once; `session_files` only names those
  files. The TUI's `orbit.tui.view.sessions`
  holds the list's presentation rules, and `orbit.tui.sessions` the modal.

## Deferred (to do later)

- **Copying tasks between sessions**, such as moving unfinished work into a new session.
- **Two orbits running tasks at once in one repository.** Locks protect sessions only. Every orbit
  on a repository shares the coder's worktree, `.git/orbit/coder`, and its branch `orbit/coder`, so
  two orbits running tasks at the same time, even in different sessions, would work in the same
  tree. Making that safe needs a lock on the worktree, or a worktree per orbit.
