# Telemetry

What orbit records as OpenTelemetry spans, the metrics it records from them, the live gauges it
reports, and what its agents send of their own, once telemetry is turned on (README.md's
**Telemetry** says how). Span names are low-cardinality and start with
`orbit.`; metric and attribute names follow OpenTelemetry's GenAI conventions where one fits
(`gen_ai.*`) and start with `orbit.` otherwise.

Nothing a person or an agent wrote travels on a span beyond what is listed here: no prompt, agent
message, diff or check output. A task's title is its prompt's first line, and its reason the one
line the task list shows.

## Tasks

### `orbit.task`

One span for each run of a task through the work lane, from when the task is ready to run (before
any wait for a coder's slot) until its status is final, after its work is merged. Each starts its
own trace: it's a root span, linked to the span that started its run (see **Links**). A task run
again, such as after the usage limit stopped it, has a span for each run; a fix task queued after
verification fails has its own.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.workspace.id` | string | The workspace's id (`unregistered` for one no store registers). |
| `orbit.session.id` | string | The id of the session open as the task started. |
| `orbit.task.id` | string | The task's id on the session's task list. |
| `orbit.task.title` | string | The task's prompt's first line, cut to 71 characters. |
| `orbit.role` | string | The role that takes the task, such as `coder` or `reviewer`. |
| `orbit.runtime` | string | What runs the agent: `claude`. |
| `gen_ai.request.model` | string | The model the task's agent is launched with; absent when the task names none (the CLI's default). |
| `orbit.trigger` | string | Who queued the task: `manual`, `planner`, `review` or `super_review`. |
| `orbit.task.fixes` | string | For a fix task, the id of the task it fixes; absent otherwise. |
| `orbit.task.fix_attempts` | int | How many times the agent was told its work failed verification and asked to fix it. |
| `orbit.task.wait_ms` | double | How long the task waited in line for a coder's slot, in milliseconds; 0 when it didn't. |
| `orbit.task.check_runs` | int | How many verification steps the agent ran itself (`run_check`). |
| `gen_ai.usage.input_tokens` | int | Input tokens over every turn run for the task. |
| `gen_ai.usage.output_tokens` | int | Output tokens over those turns. |
| `gen_ai.usage.cache_creation.input_tokens` | int | Input tokens written to the cache over those turns. |
| `gen_ai.usage.cache_read.input_tokens` | int | Input tokens read from the cache over those turns. |
| `orbit.cost.usd` | double | What those turns cost, in US dollars. |
| `orbit.verify.result` | string | How the task's last verification came out: `passed`, `failed` or `skipped`; absent when it wasn't verified. |
| `orbit.verify.failed_step` | string | The step that last failed verification, if one did, even if a later verification passed. |
| `orbit.merge.result` | string | What came of merging the task's work: `merged`, `rebased`, `conflict`, `blocked` or `detached`; absent when it wasn't merged. |
| `orbit.task.status` | string | The task's final status, such as `completed`, `unverified`, `gave_up`, `error` or `cancelled`; `pending` if the run stopped before it started. |
| `orbit.task.reason` | string | Why the task ended badly, in one line, such as `Gave up: …` or `Unit tests failed`; absent when it didn't. |

The usage, status and reason are those the task list holds as the span ends; a task removed from
it by then has none of them.

A task that ends in error, or whose run breaks on a bug, has the span's error status with its
reason as the message, and an `exception` event with that message.

Events:

| Event | Attributes | Meaning |
| --- | --- | --- |
| `orbit.verify.step` | `name` (string), `result` (string: `passed` or `failed`), `duration_ms` (double) | A verification step of the task's ran, for that long. The steps an agent runs itself aren't events. |
| `orbit.task.gave_up` | | The agent gave up; `orbit.task.reason` says why. |
| `orbit.merge.conflict` | | The task's work conflicted with the base branch. |
| `exception` | `exception.type`, `exception.message` | The task ended in error. |

### `orbit.task.wait`

A child of `orbit.task`, timing the task's wait in line for a coder's slot; only a task that
waited has one.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.wait.position` | int | The task's place in line as it started waiting, 1 next. |
| `orbit.wait.slots_busy` | int | How many coders held slots as it started waiting. |

### `orbit.task.run`

A child of `orbit.task`, timing the task's run from its agent's start until its work is settled
and merged. It has no attributes; its agent's `orbit.agent.process` span is its child.

## The TUI

### `orbit.tui.action`

One span for each thing the user does in the TUI that sends commands, from when it sends the
first until it has sent the last; every command it sends carries its trace context, so their
`orbit.command` spans are its children. An action sending several commands, such as picking a
session (`Attach`, then `ActivateSession`), is one span; an action sending nothing, such as
hiding the sidebar or escaping a picker, has none. What the TUI sends on its own, such as its
`Hello` or the `Attach` opening it on a workspace, carries no trace context. A chat joining a
control plane traces its TUI with the user config's `[telemetry]` where it runs; without one,
its commands carry none.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.action` | string | What the user did, by the TUI's own name for it: `run_tasks`, `cancel_turn`, `stop_planner`, `reset_planner`, `super_review`, `configure_checks`, `pick_model`, `pick_tool_access`, `cancel_agent`, `submit_prompt`, `add_task`, `edit_task`, `accept_task`, `remove_task`, `decide_proposal`, `pick_session`, `rename_session`, `delete_session`, `activate_session`, `pick_workspace`, `add_workspace`, `remove_workspace`, `browse`, `page_history`, `resume_storage` or `take_ownership`. |

An error escaping an action, a bug, gives its span the error status with the error's message, and
an `exception` event.

## Commands

### `orbit.command`

One span for each command a frontend sends, from when the control plane receives it until its
outcome is published, read-only ones included. It's a child of the span whose trace context the
command's envelope carries, such as the frontend's own span for what the user did; a command
carrying none starts its own trace. The span is current while the command is handled, so the work
it does there nests under it, and the events it publishes, its outcome among them, carry its
trace context.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.command.type` | string | The command's `type`, such as `add_task` or `hello`. |
| `orbit.workspace.id` | string | The id of the workspace the command is for: the one its envelope names, else the one its connection is attached to; absent when it's for none, as before the connection attaches. |
| `orbit.session.id` | string | The id of the session the command is for: the one its envelope names, else the one its connection views; absent when it's for none. |
| `orbit.client` | string | The name of the client that sent the command, as its connection's `Hello` (or a `Hello` itself) gives it; absent before one. |
| `orbit.command.outcome` | string | How the command came out: `accepted`, as a `Hello` answered by `Welcome` is, or `refused`. |
| `orbit.command.code` | string | A refused command's code, such as `busy` or `invalid`; absent when it was accepted. |

A command refused because handling it failed (`internal`) has the span's error status with the
refusal's message, and an `exception` event; other refusals aren't errors, as they're the
command's doing.

## Connections

### `orbit.connection`

One span for each WebSocket `orbit serve` accepts from a frontend, from when it's accepted until
it's closed, by either side: one that never says `Hello`, or that arrives as the server stops,
has one too. Each starts its own trace, and is never current: the commands sent over the
connection have `orbit.command` spans of their own, which aren't its children.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `network.peer.address` | string | The address the frontend connected from; absent when the server can't tell. |
| `network.peer.port` | int | The port it connected from; absent when the server can't tell. |
| `orbit.client` | string | The client's name, as the frontend's first `Hello` gives it; absent before one. |
| `orbit.protocol_version` | int | The protocol version that `Hello` speaks, even one this orbit doesn't, which ends the connection. |

Events:

| Event | Attributes | Meaning |
| --- | --- | --- |
| `hello` | | The frontend sent its first `Hello`; a second, which is refused, isn't one. |
| `reconnect` | `since` (int), `gap` (int) | That `Hello` gave `since`, the last `seq` the frontend saw, to catch up from; `gap` is how many `seq` had been stamped after it as the `Hello` arrived, negative when the frontend's is ahead of this orbit's, which sends it a snapshot instead. |
| `attach` | `orbit.workspace.id` (string), `orbit.session.id` (string) | An `Attach` was accepted: the frontend views that session of that workspace. |
| `detach` | | The frontend left the workspace it was attached to, as it attached to another or the workspace stopped being served. |
| `close` | `code` (int), `reason` (string) | The WebSocket closed with that code and reason: the frontend's, if it closed it, else the server's, such as `1001` as the server shuts down or `1008` for a frame it couldn't read; `1006` when the frontend went without a close frame. |

## The planner

### `orbit.planner.turn`

One span for each turn of the planner's conversation, from when the turn starts in the planner's
lane until it ends, its duration the span's own. It's a child of the `orbit.command` span of the
`SendMessage` asking for it, and current while the turn runs; the planner's `orbit.agent.process`
span for the turn is its child. A message refused, as one sent while the planner answers, starts
no turn and has none.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.workspace.id` | string | The workspace's id (`unregistered` for one no store registers). |
| `orbit.session.id` | string | The id of the session open as the turn started. |
| `gen_ai.request.model` | string | The model the planner is launched with: the session's, else the planner role's; absent when neither names one (the CLI's default). |
| `gen_ai.usage.input_tokens` | int | Input tokens of the turn. |
| `gen_ai.usage.output_tokens` | int | Output tokens of the turn. |
| `gen_ai.usage.cache_creation.input_tokens` | int | Input tokens the turn wrote to the cache. |
| `gen_ai.usage.cache_read.input_tokens` | int | Input tokens the turn read from the cache. |
| `orbit.cost.usd` | double | What the turn cost, in US dollars. |
| `orbit.planner.drafts` | int | How many draft tasks the planner proposed in the turn. |
| `orbit.planner.end_reason` | string | How the turn ended: `completed`, `cancelled`, `max_steps` or `error`. |

The usage is what the CLI reported for the turn, and 0 when it reported none, as for a turn
cancelled before it finished.

A turn that fails, such as by running out of the planner role's time, or that the CLI reports an
error for has the span's error status with the error's message; a failure also records an
`exception` event.

## Reviews

### `orbit.review`

One span for each review the review lane runs, a reviewer's or a super reviewer's, from when it
starts until it's followed up: its batch's review queued, if that's due, or the work lane asked
to run its suggestions. Each starts its own trace: it's a root span, linked to what queued it (see
**Links**). The span is current while the review runs and is followed up; the reviewer's
`orbit.agent.process` span is its child.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.workspace.id` | string | The workspace's id (`unregistered` for one no store registers). |
| `orbit.session.id` | string | The id of the session open as the review started. |
| `orbit.role` | string | The review's role: `reviewer`, or `super-reviewer` for a super review. |
| `orbit.review.commits` | int | How many commits the review reads: 1 for a commit's own review or a super review (of the codebase at one commit), the batch's for a batch's review. |
| `orbit.review.round` | int | The round of the work reviewed: 0 for a commit of the user's or the planner's task, and for a super review; one more for each round of review-suggested work. |
| `orbit.review.batch` | string | The batch the review's suggestions join, such as `batch-2`. |
| `orbit.review.cap` | int | The most suggestions the review may make. |
| `orbit.review.suggestions` | int | How many of the review's suggestions were queued as tasks. |
| `orbit.review.refused` | int | How many were refused: over the cap, repeating a task or an earlier suggestion, once the review's batch was closed, or made by a review the user queued, which can't suggest. |
| `gen_ai.usage.input_tokens` | int | Input tokens over every turn of the review's agent. |
| `gen_ai.usage.output_tokens` | int | Output tokens over those turns. |
| `gen_ai.usage.cache_creation.input_tokens` | int | Input tokens written to the cache over those turns. |
| `gen_ai.usage.cache_read.input_tokens` | int | Input tokens read from the cache over those turns. |
| `orbit.cost.usd` | double | What those turns cost, in US dollars. |

A review the user queued, rather than orbit, reviews no particular commits: it has no
`orbit.review.commits`, `orbit.review.round`, `orbit.review.batch` or `orbit.review.cap`. The
usage is the task list's as the span ends; a review removed from it by then has none.

A review that ends in error, or whose run breaks on a bug, has the span's error status with its
reason as the message, and an `exception` event with that message.

Events:

| Event | Attributes | Meaning |
| --- | --- | --- |
| `orbit.task.gave_up` | | The review's agent gave up. |
| `exception` | `exception.type`, `exception.message` | The review ended in error. |

## Agents

### `orbit.agent.process`

One span for each agent process orbit runs, from its start until it exits: a task's agent, a
child of the task's `orbit.task.run`; the planner's, one for each turn's process, a child of the
`orbit.planner.turn`; and a reviewer's, a child of the `orbit.review`. A planner turn whose saved
conversation couldn't be resumed runs a second process, and has a span for each.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.agent.id` | string | The agent's id, such as `agent-3`; the planner's is the same for each of its turns. |
| `orbit.role` | string | The agent's role, such as `coder`, `planner` or `reviewer`. |
| `orbit.runtime` | string | What runs the agent: `claude`. |
| `orbit.agent.exit_reason` | string | How the process ended: `finished`, `crashed` (it failed, or a turn ended in error) or `killed` (orbit stopped it, as when the user stops the task or cancels the turn). |
| `orbit.agent.turns` | int | How many turns the process started. |
| `orbit.agent.tool_names` | string[] | The tools the agent called, by name as the CLI reports them (`Bash`, `Edit`, `mcp__orbit__get_task`, …), sorted. |
| `orbit.agent.tool_counts` | int[] | How many times it called each of those tools, in the same order. |
| `orbit.agent.tool_calls_total` | int | How many tool calls it made in all. |

A process that fails, rather than ending a turn in error, has the span's error status with the
failure's message, and an `exception` event.

### `orbit.agent.request`

One span for each call an agent makes to one of orbit's tools (`get_task`, `complete_task`,
`run_check`, `report_progress`, `propose_task` and so on), from when orbit receives it until it
replies; its duration is the span's own. It's a child of the calling agent's
`orbit.agent.process` span, though orbit's MCP server handles the call apart from the agent's
work, and current while the call is handled, so the events it publishes carry its trace context.
A call arriving once the agent's process has ended is a child of the span current, if any. A call
with a token orbit doesn't know has no span. Neither the call's arguments nor its reply are
recorded.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.agent.tool` | string | The tool called, such as `complete_task`. |
| `orbit.agent.request.error` | boolean | Whether orbit's reply was an error, such as a refusal or the reply to a call orbit stopped handling; a check that ran and failed isn't one. |

A `complete_task` call's span lasts until its claim is verified, as the agent waits that long
for the reply.

### `orbit.verify.step`

One span for each verification step run for an agent's call, a child of that call's
`orbit.agent.request` span: each step verifying a `complete_task` claim, and the step a
`run_check` runs. Its duration is the step's own. The steps verifying a task are still events of
its `orbit.task` span too; the startup check's steps are children of its `orbit.startup_check`
span (see **The startup check**); other steps run with no call to answer, as for an agent that
exited without claiming its task done or for checks a configurator proposes, have no span.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.verify.step.name` | string | The step's name, such as `Unit tests`. |
| `orbit.verify.step.result` | string | How it came out: `passed` or `failed`; absent when it was cut short. |

## The startup check

### `orbit.startup_check`

One span for each run of the startup check, which runs the verification steps once on the base
commit as a workspace's background work starts, from its start until it ends. Each starts its own
trace: it's a root span, though what starts the check, such as the first frontend's `Attach`, may
be a command's. The span is current while the check runs, so the events it publishes carry its
trace context, and each step it runs, the tree's setup (`setup`) first if there is one, is an
`orbit.verify.step` child, timed and with its result as for an agent's call.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.workspace.id` | string | The workspace's id (`unregistered` for one no store registers). |
| `orbit.verify.result` | string | How the check came out: `passed`, `failed`, or `cancelled`, as when the user cancels its agent or orbit closes meanwhile; absent when it couldn't run. |
| `orbit.verify.failed_step` | string | The step that failed, `setup` for the tree's setup; absent unless one did. |
| `orbit.startup_check.fix_added` | boolean | Whether it added a draft task to fix the step that failed. |

A check that couldn't run, as when git couldn't make its snapshot of the base, or that breaks on
a bug, has the span's error status with the error's message, and an `exception` event.

## Links

Work that runs on in the background starts its own trace and links to the span that started it,
so no trace stays open for hours while each can still be followed back to its cause.

- Each `orbit.task` span links to the span that started its run: the `orbit.command` span of the
  `RunTasks` asking for it, or of the `FireTrigger` queueing a configurator, or the
  `orbit.review` span of the review whose suggestions the run was started for. A run the usage
  limit stopped keeps its link when it starts again by itself, at the reset or once a halt
  lifts; work asked for while another orbit owned the repository, and run once this one owns it,
  links to its own command's span. A run started outside any span has no link; `orbit.trigger`
  still says who queued each task.
- Each `orbit.review` span links to the span current as orbit queued the review: the
  `orbit.task` span of the task whose landing made it due (a commit's own review, or a batch's
  once its last task landed) or whose ending without a commit finished its batch; for a super
  review, the `orbit.command` span of the `FireTrigger` asking for it, or the `orbit.task` span of
  the landing whose count made it due; or the `orbit.review` span of the review whose end
  finished its batch. A review the user queued, and one queued before the session was last
  opened, has no link.

## Trace context

Each event published, a workspace's or the control plane's own, carries, in its envelope's
`traceparent` and `tracestate`, the trace context of the span current as it's published, so what a
frontend shows can be tied to the span that caused it. An event published outside any span
carries none.

## Metrics

Orbit records metrics from its spans as each ends, and exports them where the spans go every
`metrics_interval_s` seconds, and once more as it closes. Counters and histograms are cumulative
over each run of orbit, its `service.instance.id`. Each takes its attributes from its span: a span
lacking one records without it, as a planner turn, which has no `orbit.role`, does. A count of 0,
or none, isn't recorded. Metrics carry no exemplars.

| Metric | Kind | Unit | Attributes | Source |
| --- | --- | --- | --- | --- |
| `orbit.tasks` | counter | `{task}` | `orbit.task.status`, `orbit.role`, `orbit.workspace.id` | 1 for each `orbit.task` span. |
| `orbit.verify.step.duration` | histogram | `s` | `orbit.verify.step.name`, `orbit.verify.step.result` | The duration of each `orbit.verify.step` span. |
| `orbit.agent.duration` | histogram | `s` | `orbit.role`, `orbit.runtime` | The duration of each `orbit.agent.process` span. |
| `gen_ai.client.token.usage` | histogram | `{token}` | `gen_ai.request.model`, `gen_ai.token.type`, `orbit.role` | One record for each kind of token an `orbit.task`, `orbit.planner.turn` or `orbit.review` span counts, `gen_ai.token.type` naming it: `input` (`gen_ai.usage.input_tokens`), `output` (`gen_ai.usage.output_tokens`), `cache_creation` (`gen_ai.usage.cache_creation.input_tokens`) or `cache_read` (`gen_ai.usage.cache_read.input_tokens`). |
| `orbit.cost` | counter | `USD` | `gen_ai.request.model`, `orbit.role` | The `orbit.cost.usd` of those spans. |
| `orbit.review.suggestions` | counter | `{suggestion}` | `orbit.review.outcome`, `orbit.role` | The `orbit.review.suggestions` of each `orbit.review` span, with `orbit.review.outcome` `made`, and its `orbit.review.refused`, with `refused`. |

`gen_ai.client.token.usage` has the buckets the GenAI conventions give it, 1, 4, 16 and so on, each
4 times the last, to 67108864; the durations have the SDK's default buckets, 0 to 10000 seconds.

### Gauges

Orbit also reports live gauges, which no span can give: each is read from what the control plane
holds as the metrics are collected, every `metrics_interval_s` seconds and once more as it closes,
and exported with them. A gauge whose read fails reports nothing that time, and orbit logs a
warning, once until it reads again; the other gauges still report.

| Metric | Kind | Unit | Attributes | Source |
| --- | --- | --- | --- | --- |
| `orbit.agents.active` | gauge | `{agent}` | `orbit.role`, `orbit.workspace.id` | The agents running in each workspace served, by role, as their `AgentStarted` and `AgentExited` events tell it, as `AgentsChanged` does: a value for each role the workspace configures, 0 while none runs, and for any other role while one runs, such as the startup check's `checker`. |
| `orbit.queue.depth` | gauge | `{task}` | | How many tasks wait in line for a coder's slot (`max_coders`), across the workspaces served. |
| `orbit.tasks.pending` | gauge | `{task}` | `orbit.workspace.id` | How many tasks are `pending` in each workspace's open session. |
| `orbit.frontends.connected` | gauge | `{connection}` | | How many frontends are connected whose `Hello` was welcomed, attached to a workspace or not. |
| `orbit.event_log.size` | gauge | `By` | `orbit.workspace.id` | The bytes the logs of each workspace's sessions hold in the store, their events' JSON, as the store last knew them: as it opened, and after each write to them since; 0 for a workspace with no store. |

## Agents' own telemetry

With the OTLP exporter, each agent's Claude Code CLI sends its own metrics and logs, as Claude
Code's monitoring documentation describes them, to the collector orbit's spans go to, over the
same protocol; README.md's **Telemetry** lists the variables it's given. Its resource carries these
attributes beside the CLI's own:

| Attribute | Type | Meaning |
| --- | --- | --- |
| `orbit.task.id` | string | The id of the task the agent works on, as on its task's `orbit.task` span; absent for the planner, which has none. |
| `orbit.agent.id` | string | The agent's id, as on its `orbit.agent.process` span. |
| `orbit.role` | string | The agent's role, such as `coder`, `planner` or `reviewer`. |
| `orbit.workspace.id` | string | The id of the workspace it works in. |

No headers are passed, so a collector that needs them receives none of it. With the console or
file exporter, agents send nothing.
