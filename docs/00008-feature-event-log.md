# Event log requirements

The decisions and plan for phase 2b of milestone 2 (store and event log) of the control-plane design: every event of a session is kept in the store, which makes it the transcript: replayed on reconnect, shown on joining, paged through, bounded by retention settings, and never silently lost when storage fails.

## Goal

A session's conversation survives restarts and switches: the TUI shows where a session left off, can page back through all of it, and a reconnecting client gets exactly what it missed. When storage fails the session halts with an explanation the user can act on, unsaved events are parked on disk, and resuming makes the log complete or honest about any gap.

## The log

1. Every event that belongs to a session is appended to the store's `events` table, keyed by (workspace, session, `seq`), with its type, JSON and size; excluded are `SESSIONLESS_EVENTS` and the events that answer one connection: `Welcome`, `Catalog`, `Snapshot`, `HistoryPage` (`CONNECTION_EVENTS` in `orbit.protocol`). Outcomes are kept. Over only conversation events: replay couldn't rebuild state; over everything: replaying another client's `Welcome` or `Snapshot` makes no sense.
2. `seq` stays an in-memory counter, started from the store's highest `seq` when the store opens; processes sharing the database count independently, and replay never mixes sessions. Over store-assigned `seq`: stamping would become async at every publishing site; over restarting at 1: a `since` from before a restart would point at the wrong events.
3. Write-behind: a writer task appends logged events in order, batching what has queued into one transaction; delivery never waits on the disk; closing flushes the queue.
4. Deleting a session deletes its events; sessions imported in phase 2a start with an empty log.

## Storage halt

5. When an event-log write or a session-record save fails, the session halts: running agents, the planner's turn and reviews are cancelled, and every command but `ResumeStorage` and quitting is refused with the new code `storage_failed`, which the halting `ErrorOccurred` carries too; `save_failed` stays for compatibility. Over carrying on: work would go unrecorded; over exiting: the user loses sight of their work; over halting only on log failures: disk-full usually breaks both, and two messages would describe one fault.
6. The explanation is one text built in the domain. It reports which write failed, the database path and the SQLite error as reported; the unsaved events' first and last `seq`; the cause orbit diagnosed from the error class, with its fix (disk full: free space; read-only or permission denied: check the state directory's owner and mode; locked beyond the busy timeout: another process holds the database; file missing: the state directory was moved or removed), saying so when the error is one it doesn't recognise; where the unsaved events were written; and that saved data is intact up to the last saved event, and how to resume. Chosen over the error and path alone, which would leave the user to work out the cause.
7. Unsaved events are written, one encoded envelope per line and owner-only, to `<state dir>/fallback/<workspace id>-<session id>.jsonl`, else `.git/orbit/fallback/<session id>.jsonl` (`.orbit/fallback/` outside git); the message names the file used, or says neither could be written. Over the temporary directory: wiped on reboot; over `.git/orbit` only: store data mixed into the workspace.
8. `ResumeStorage()` retries the failed writes; when they work it appends the fallback file's events, then those still queued, in `seq` order in one transaction, deletes the fallback file, lifts the halt and answers `CommandAccepted`. Events that reached neither the log nor a fallback are recorded as `LogGap(first_seq, last_seq)`, shown by the transcript and `History`. Still failing, it is refused with `storage_failed` and the fresh explanation. Over appending silently: `History` would have holes; over keeping the fallback file: events could be imported twice.
9. On every start, after the store opens, fallback files for this workspace in both places are imported as in 8 and removed, and a Notice after the Snapshot names the file and how many events were recovered; a failed import exits `orbit chat` with status 2 naming the file and the error. Over a Notice only: the gap would wait silently.
10. The TUI shows the halt as a modal with the full text, "Try again" (sending `ResumeStorage`) and "Close"; a persistent status line by the prompt ("Session stopped: event log failed; see the transcript"); the error in the transcript; and the text printed to the terminal on exit. Any command refused with `storage_failed` reopens the modal. Over a transcript line alone: easy to scroll past; over a key to reopen it: one more thing to learn when any refused command brings it back.
11. The Snapshot carries the halt (`storage_halt: str | None`, the explanation), and the TUI opens the modal when a Snapshot has one.

## Replay and the tail

12. `Hello(since=N)` replays when every logged event after N belongs to the open session and N is no older than that session's oldest kept event: `Welcome`, `Catalog`, the latest usage, the logged events after N in order, one-off notices, live events. Otherwise a fresh Snapshot. Over replaying across sessions: a mixed history; over always a Snapshot: `since` stays dead.
13. `Snapshot.transcript: tuple[EventEnvelope, ...]`: the session's last 500 logged events, oldest first. Over the last N turns: one long turn could make a huge Snapshot; over the last N bytes: a cut could land mid-turn.
14. The TUI replaces its transcript with the Snapshot's tail on every Snapshot: startup shows where the session left off, a switch shows the session switched to. A visible change. Over startup only: a switch would show the old session under the new title; over ignoring the tail: the log unused where people look.
15. `History(session, before_seq, limit)` (limit at most 500) is answered by `HistoryPage(session, events, more, trimmed)`, oldest first; the TUI fetches the previous page when the user moves past the top of the conversation in list mode, showing "Loading earlier messages…". Over protocol only: TUI users would see only 500 events; over a jump to the start: thousands of events at once.

## Retention

16. `tool_output_kb` (default 64; 0 means unlimited) cuts any text field of a stored event above the limit (tool outputs and arguments, assistant messages, agent replies) to its first and last half with a marker naming the size cut; live events are never cut. Over outputs only: large file writes stay whole; over cutting live events: the user loses what they'd see now.
17. `session_log_mb` (default 200; 0 means unlimited) is checked after each batch: over it, the session's oldest events are deleted until under 90% of it. `HistoryPage.trimmed` marks the oldest page when earlier events were removed, and the TUI shows "Older events were removed (session_log_mb)". Over trimming at startup only: unbounded growth; over no marker: the start silently disappears.
18. The user config is `<config dir>/config.toml`, the config directory from `--config-dir`, else `ORBIT_CONFIG_DIR`, else `$XDG_CONFIG_HOME/orbit`, else `~/.config/orbit`. A missing file or `[storage]` means the defaults; an unknown key or a bad value in `[storage]` exits with status 2 naming the file and the key; other top-level tables are ignored. Over rejecting unknown tables: an older orbit would break on milestone 3's `[server]`; over ignoring bad values: a typo silently keeps everything.

## Protocol and docs

19. New in `orbit.protocol`: `CONNECTION_EVENTS`; the commands `History` and `ResumeStorage`; the events `HistoryPage` and `LogGap`; `Snapshot.transcript` and `Snapshot.storage_halt`; the code `storage_failed`. Each task adding them regenerates the schema and updates `docs/protocol.md`.
20. README's **Where orbit keeps things** gains the event log as the transcript, `[storage]` with its defaults, `--config-dir`/`ORBIT_CONFIG_DIR`, and a link to a new **Storage failures** section (what stops, the message, the fallback locations, "Try again", recovery at startup); the Sessions section notes that switching shows the other session's conversation. Over one section: harder to find by searching.

## How it will be built

1. Write this record.
2. Append session events to the store: the `events` table (migration 2), `CONNECTION_EVENTS`, the `EventLog` port and its SQLite implementation, `LogWriter` fed by `Outbox`, `seq` started from the store; write failures reported once as `save_failed` until the halt lands.
3. Send the transcript tail in the Snapshot: `EventLog.recent`, `Snapshot.transcript` (stored plus still-queued events), the TUI replacing its transcript on every Snapshot.
4. Page through a session's history: `History`, `HistoryPage`, `EventLog.before`, TUI paging past the top in list mode.
5. Replay missed events on `Hello(since)`.
6. Read `[storage]` and cut long fields in stored events: `resolve_config_dir`, `--config-dir`, `StorageSettings`, `cut_fields`.
7. Trim session logs past `session_log_mb`: sizes, trimming after each batch, `HistoryPage.trimmed`, the TUI's marker.
8. Halt the session when storage fails: `STORAGE_FAILED`, `storage_halt_explanation`, the halt and its refusals, fallback files, `Snapshot.storage_halt`, README's **Storage failures**.
9. Resume storage and recover fallback files: `ResumeStorage`, `LogGap`, recovery at startup.
10. Show storage halts in the TUI: the modal, the status line, reopening on refusals, printing on exit.

## Out of scope

Several hosts sharing one database; exporting a session (deferred in the design).
