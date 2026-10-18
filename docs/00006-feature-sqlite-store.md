# SQLite store requirements

The decisions and plan for phase 2a of milestone 2 (store and event log) of the control-plane design: orbit's saved sessions and review counts move from per-repository files into one SQLite store per control-plane user.

## Goal

Sessions, renames, deletes, locks and the super-review count behave as today, but are kept in one database in the state directory, keyed by workspace, ready for the event log (phase 2b) and many workspaces (milestone 4). A repository with old session files starts with them imported; two repositories keep separate sessions.

## Scope

1. Milestone 2 ships as two phases: 2a the store (this record); 2b the event log (appending, `seq` resumed from the store, replay on `since`, the transcript tail in Snapshot, `History`, `[storage]` retention, `--config-dir`). Over one plan: about 22 questions in one chain; over three phases: backup and README too small to stand alone.

## Data

2. One SQLite database per control-plane user, the account running the backend, at `<state dir>/orbit.db`, in WAL mode with a busy timeout, behind the existing `SessionStore` port; a new adapter replaces `SessionFiles`. In 2a that's the user running `orbit chat`; from milestone 3 the user running `orbit serve`, whose database a standalone `orbit chat` of the same account shares; remote frontends never touch it.
3. The state directory is `--state-dir`, else `ORBIT_STATE_DIR`, else `$XDG_STATE_HOME/orbit`, else `~/.local/state/orbit`; it is created owner-only (0700), the database 0600.
4. A `workspaces` table (id, path, fingerprint, name) registers the current repository on start; the fingerprint is the root commit hash and the `origin` URL (none outside git or before the first commit); the name is the directory's name. Workspaces are looked up by path; re-attaching by fingerprint waits for milestone 4. The envelope's `workspace` stays `"default"`. Over keying by path: a moved repository loses its sessions and milestone 4 needs a migration; over a database per repository: contradicts one store per user; over not recording fingerprints: milestone 4 would find nothing to match.
5. A `sessions` row (`workspace_id`, `id`, `name`, `last_active`) holds the record as the JSON text `record_json` writes, so unreadable records keep their raw text; the key is (`workspace_id`, `id`); listing shows the current workspace's sessions only. Over normalised tables: a large schema to migrate with every record change; over a bare blob: listing parses every record.
6. A `reviews` table holds each workspace's commits since the last super review, replacing `reviews.json`; the review lane's state stays in the session record.
7. Session locks stay `flock`ed lock files, moved to `<state dir>/locks/<workspace id>/<session id>.lock`. Over lock rows in SQLite: a crash leaves stale rows; over leaving them in `.git/orbit/sessions`: that folder is renamed on import.
8. Stdlib `sqlite3`, on one connection owned by a one-thread executor inside the adapter, through which every store call goes. Over `aiosqlite`: below 1.0, one maintainer, recent minor releases changed behaviour; over a connection per call: slower, WAL checkpoints harder to reason about.
9. A `schema_version` table and forward-only migrations, each run in a transaction at startup. A newer database, a failed migration or an unopenable database stops `orbit chat` before the TUI with exit status 2 and a message naming the versions, or the file and the error. Over opening read-only: nothing could be saved; over a temporary database: sessions silently missing.

## Import

10. On start, the repository's `.git/orbit/sessions/*.json` and `reviews.json` (`.orbit/sessions` outside git) are imported in one transaction: unreadable records copied as raw text, existing rows never overwritten; on success the folder becomes `sessions.imported`, and a Notice after the Snapshot says so.
11. If any old session's lock is held by a running orbit, or an I/O error stops the import, `orbit chat` exits with status 2 before the TUI with a message naming the path and the cause, and nothing is imported. Over postponing with a Notice: the problem could go unnoticed; over importing what can be read: old sessions could vanish from view.

## Backup

12. `orbit db backup <path>` copies the database with SQLite's online backup, safe while orbit runs; refuses an existing target unless `--force`; writes 0600; takes `--state-dir`. Over always overwriting: risky; over a timestamped name: not the file asked for.

## Interactions and wording

13. `--state-dir` and `ORBIT_STATE_DIR` land now; `--config-dir` and `ORBIT_CONFIG_DIR` land in 2b with `[storage]`, their first reader. Over now: a directory nothing uses; over milestone 3: `[storage]` would have no override.
14. `.git/orbit` keeps only the coder worktree, review snapshots and leftovers; worktree and snapshot behaviour is unchanged.
15. README gains **Where orbit keeps things**: the five homes, the paths and overrides, the import, backup and restore (stop orbit, replace the file, start), and that the schema is readable with `sqlite3` but not an API; its Sessions, Where tasks run, Architecture and Usage passages are updated as the design lists. Retention settings are 2b's.

## How it will be built

1. Write this record.
2. Add the SQLite store adapter: `SqliteStore` implementing `SessionStore` in `orbit.agent.external.sqlite_store`, with the one-thread executor, WAL, migrations, the `workspaces`, `sessions` and `reviews` tables, flock locks under `<state dir>/locks/`, and `StoreError`; `workspace_fingerprint` in `orbit.agent.external.git_tree`. Not wired yet.
3. Import today's session files into the store: `import_session_files` copies the old sessions and `reviews.json` into a `SqliteStore` in one transaction and renames the folder; a held lock or an I/O error raises `ImportBlocked` with nothing imported.
4. Keep sessions in the store: `resolve_state_dir` and `orbit chat --state-dir`; open the store and import at startup, exiting with status 2 on `StoreError` or `ImportBlocked`; `AgentSession`'s `startup_notes` show the import Notice after the Snapshot; `SessionFiles` kept only for reading during the import; the README passages.
5. Add `orbit db backup`: `SqliteStore.backup`, the command with `--force` and `--state-dir`, and README's backup and restore.

## Out of scope

The event log and everything in phase 2b; re-attaching moved repositories and many workspaces (milestone 4); several control planes sharing one database (a server database behind the port, plus locks, `seq` and live events across hosts; not planned).
