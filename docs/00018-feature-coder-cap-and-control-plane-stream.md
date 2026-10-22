# Coder cap and control-plane stream requirements

The decisions and plan for phase 4c of milestone 4 (workspaces) of the control-plane design: a cap on coders running at once across the workspaces a control plane serves, an opt-in stream of what runs across them with usage totals, and the decision to drop unloading idle sessions.

## Goal

A server with many workspaces never runs more coders than it's set to, takes turns between workspaces fairly, says why work waits, and lets a frontend see everything running and what it has cost.

## The coder cap

1. `[server] max_coders` (default 4; 0 means unlimited) caps runs of coder and configurator agents across every workspace a control plane serves: tasks, fix tasks and startup-check fixes. Reviews, super reviews and the planner ride along uncounted. A run paused at the usage limit (`docs/00016-bugfix-fix-chains-and-usage-limits.md`) holds no slot. Over every agent: planner chats would wait behind other workspaces' tasks; over coders only: the configurator also runs checks and counts.
2. Each task takes a slot when its agent starts and gives it back when the task ends, verification included. Waiting tasks queue across workspaces in the order they became ready; a workspace's next task joins the back, so workspaces take turns. Over holding a slot for a whole run: one long queue starves the others; over priorities: a setting nobody asked for.
3. Cancelling a waiting run (ctrl+s) leaves the queue and holds the run as today; removing a workspace or switching its session drops its place. A local `orbit chat` serves one workspace, so the cap never makes it wait.
4. `Activity` gains the kind `waiting_for_coder` with `position` (1 = next), carried by `SessionStateChanged`. The TUI shows it in the session's state ("waiting for a coder: 2nd in line"), in the view's mark, and on the task row about to run ("⏳ waiting: 3 coders busy"). Over a new event: a second way to learn a session's state; over a Notice: it scrolls away and the end goes unsaid.

## The control-plane stream

5. `Watch(streams: tuple[str, ...])` sets the optional streams a connection receives, replacing the previous set: `("control_plane",)` on, `()` off; an unknown name is refused with `unknown`, naming the known ones. Over two commands per stream: the command list grows with every stream; over a boolean: covers one stream.
6. The control-plane stream carries two session-less, unlogged events: `AgentsChanged(agents)`, every agent running in any workspace (workspace, session, agent id, role, task id), sent whole on each start or exit and when watching begins; and `UsageTotals`, per workspace and in total, today's (in the server's local time) and lifetime tokens by kind and cost, sent when watching begins and after each finished turn. Workspaces and sessions changing keep reaching everyone as today (Catalog, `SessionStateChanged`). Over always on: every TUI gets every agent change.
7. A `usage` table (migration 7) records each finished turn: workspace, session, role, model, tokens by kind, cost and time. Lifetime totals add the session records' spend from before the table existed. Over lifetime only: no "today"; over memory: totals reset on restart.
8. The TUI's workspace picker watches while it's open, showing each workspace's running agents and waiting position on its row ("2 coders · 1 reviewer", "waiting: 2nd"), and doesn't watch otherwise. Over a footer line: one more line and constant streaming; over not using it: no view across workspaces.

## Unloading

9. Unloading idle sessions is dropped from the design: an idle open session costs a few objects in memory, and reloading would complicate every path that assumes it's loaded (usage-limit waits, reviews, attach). Revisit only on a measured need. Over unloading after 30 idle minutes: load and unload paths for very little; over deferring again: it hangs a third time.

## Docs

10. The schema is regenerated; `docs/protocol.md` covers `Watch`, `AgentsChanged`, `UsageTotals` and `waiting_for_coder`; README covers `max_coders`, waiting, and the picker's activity.

## How it will be built

1. Write this record.
2. Cap coder runs across workspaces: `CoderSlots` in `ControlPlane`, `[server] max_coders`, `waiting_for_coder` in `Activity`.
3. Show waiting for a coder in the TUI.
4. Record each turn's usage: migration 7's `usage` table and `UsageLedger`.
5. Watch the control plane: `Watch`, `AgentsChanged`, `UsageTotals`.
6. Show workspace activity in the picker.
