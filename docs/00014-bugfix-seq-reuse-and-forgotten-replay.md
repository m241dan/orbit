# Seq reuse and forgotten-session replay requirements

The decisions and plan for fixing two faults of the event log (phase 2b): `seq` numbers were reused after a restart, and a frontend that last saw a forgotten session's events was replayed another session's events instead of getting a fresh Snapshot.

## Goal

Every `seq` is handed out once, ever, and `Hello(since)` replays only when nothing but the open session happened after `since`, even when the events of the session the frontend last saw have since been deleted.

## Cause

Orbit forgets (deletes) the logged events of a session that was never saved: whenever the user switches away from the unsaved interim session, and when orbit closes with it open. The store resumed `seq` from the highest event still stored, so forgotten numbers were handed out again after a restart. And replay decides that another session's events came after `since` from the events still stored, so once a session's events are forgotten, a frontend that last saw them (for example after `orbit serve` restarts) is replayed the open session's later events on top of a view of a session that no longer exists.

## Behaviour

1. The store keeps a high-water mark: the highest `seq` ever appended, across workspaces. `seq` resumes from it when the store opens, so forgetting or deleting events never lowers it. Migration 4 adds it, starting at the highest `seq` stored.
2. The store also keeps, per workspace, the highest `seq` it has forgotten (`forgotten_seq`), raised whenever a session's events are forgotten or deleted. The log bounds count it as another session's event: `others_seq` is the higher of the highest kept event of any other session and `forgotten_seq`. `can_replay` and `LogBounds` keep their meaning and shape. Over a new `LogBounds` field and `can_replay` rule: the forgetting would spread over the domain rule, the bounds and the store; over accepting the replay: a server restart shows the wrong session's view.
3. Trimming (`session_log_mb`) doesn't raise `forgotten_seq`: trimmed events are the session's own, already recorded as its `trimmed_seq`.

## How it will be built

1. Write this record.
2. Stop reusing seq numbers and replaying after forgotten sessions: migration 4 with the mark and `forgotten_seq`; the store's counter, appends, forgetting and deleting; `_bounds`; the upgrade tests; `docs/protocol.md`.
