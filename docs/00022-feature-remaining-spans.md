# Remaining spans requirements

The decisions and plan for phase 5b of milestone 5 (telemetry) of the control-plane design: spans for frontend actions and commands, planner turns, reviews, agent processes with their requests and verification steps, the startup check and server connections, and the trace context that ties them together across processes.

## Goal

One trace reads from the key the user pressed to the command it sent; every task, review and turn points back to what started it; and every unit of work the design lists arrives as one wide span.

## Relations and context

1. Long or background work starts its own trace and links to what started it: tasks to the command that started their run, reviews to the landing task, super reviews to their trigger. Short synchronous work stays inside its command's span; a planner turn is a child of its `SendMessage` command. Over children for everything: forty-task traces open for hours; over no relations: the end-to-end trace never connects.
2. A run remembers the command span that started it (`RunTasks`, or `FireTrigger` for the configurator), and its tasks link there, including after a usage-limit restart; a run started by a review's queued work links to that review; a run with neither has no link, and `orbit.trigger` still says why. Over linking direct starts only: a restart loses the link; over a synthetic resume span: one more span kind.
3. The `Telemetry` port gains links on `start`, and injecting into and extracting from an envelope (`carry`, `carried`), all no-ops in `NoTelemetry`.
4. The `Outbox` stamps each event's envelope with the current span's context as it's published. Over outcomes only: other events can't be traced; over none: a designed feature unbuilt.

## The TUI

5. The TUI starts an `orbit.tui.action` span for each user action that sends commands, and carries its context in those commands' envelopes; an action sending several commands (picking a session: `Attach`, then `ActivateSession`) is one span with each command under it; actions that send nothing get none. The TUI exports with the frontend user's own `[telemetry]` through `orbit.telemetry`; without it, it sends no context. Over no TUI spans: no client view and no "key pressed" link; over the server's settings sent in `Welcome`: a frontend may not reach that collector.
6. `orbit.action` names the TUI's own action: `run_tasks`, `cancel_turn`, `stop_planner`, `reset_planner`, `super_review`, `configure_checks`, `pick_model`, `pick_tool_access`, `cancel_agent`, `submit_prompt`, `add_task`, `edit_task`, `accept_task`, `remove_task`, `decide_proposal`, `pick_session`, `rename_session`, `delete_session`, `activate_session`, `pick_workspace`, `add_workspace`, `remove_workspace`, `browse`, `page_history`, `resume_storage`, `take_ownership`. Over command types as names: the TUI's own names were chosen.

## The control plane's spans

7. Every command gets `orbit.command` with `orbit.command.type`, the outcome (`accepted` or `refused`, with the code), workspace and session ids and the client name; a child of the carried action span when there is one. Over skipping read-only commands: a slow listing or history page invisible.
8. `orbit.planner.turn`: workspace, session, model, tokens (`gen_ai.usage.*`), `orbit.cost.usd`, duration, drafts proposed, end reason.
9. `orbit.review` covers reviews and super reviews (`orbit.role` tells them apart): commits, round, batch, suggestions made and refused, cap, tokens, cost, duration.
10. `orbit.agent.process`, a child of the task's `orbit.task.run`, the planner turn or the review: runtime, exit reason, turns, `orbit.agent.tool_names` (sorted), `orbit.agent.tool_counts` (aligned), `orbit.agent.tool_calls_total`. Each orbit tool call is a child `orbit.agent.request` (tool name, duration, error or not; no arguments or reply text). Over an event per tool call: hundreds of events; over a key per tool: unbounded key cardinality.
11. Verification steps are child spans `orbit.verify.step` (name, result, duration) under the request that ran them (`complete_task` or `run_check`); the task span keeps its step events too. Over child spans only: querying a task's checks needs joins; over events only: the per-step child spans dropped.
12. `orbit.startup_check`, a root span per run: workspace, the steps as `orbit.verify.step` children, result, failed step, whether a fix draft was added. Over no span: slow starts untraceable; over connection events: the check isn't per connection.
13. `orbit.connection` from the WebSocket opening to its close: client name, protocol version, peer address; events `hello`, `attach`, `detach`, `reconnect` (with the `since` gap) and the close reason. Over short spans per event: the connection is never one unit; over an extra opening span: recorded twice.
14. Content rules as in phase 5a: no prompts, messages, diffs, arguments or outputs on spans; errors set the status and record an exception event.

## Docs

15. `docs/telemetry.md` gains every new span, attribute, event and link; README's **Telemetry** adds the TUI's own `[telemetry]` and how traces connect from the TUI to commands, runs and tasks.

## How it will be built

1. Write this record.
2. Give the tracing port links and trace context: `start(..., links)`, `carry`, `carried`; the `Outbox` stamping events.
3. Trace commands: `orbit.command` in `ControlPlane`.
4. Trace actions in the TUI: `orbit.tui.action` and its names; the TUI's own telemetry.
5. Trace planner turns: `orbit.planner.turn`.
6. Trace reviews and link runs to what started them: `orbit.review`; runs remembering their start; task links.
7. Trace agent processes and their requests: `orbit.agent.process`, `orbit.agent.request`, `orbit.verify.step` spans.
8. Trace the startup check and server connections: `orbit.startup_check`, `orbit.connection`.
