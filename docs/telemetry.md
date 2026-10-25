# Telemetry

What orbit records as OpenTelemetry spans once telemetry is turned on (README.md's
**Telemetry** says how). Span names are low-cardinality and start with `orbit.`; attribute names
follow OpenTelemetry's GenAI conventions where one fits (`gen_ai.*`) and start with `orbit.`
otherwise.

Nothing a person or an agent wrote travels on a span beyond what is listed here: no prompt, agent
message, diff or check output. A task's title is its prompt's first line, and its reason the one
line the task list shows.

## Tasks

### `orbit.task`

One span for each run of a task through the work lane, from when the task is ready to run (before
any wait for a coder's slot) until its status is final, after its work is merged. Each starts its
own trace: it's a root span. A task run again, such as after the usage limit stopped it, has a
span for each run; a fix task queued after verification fails has its own.

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
and merged. It has no attributes.

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

## Trace context

Each event published, a workspace's or the control plane's own, carries, in its envelope's
`traceparent` and `tracestate`, the trace context of the span current as it's published, so what a
frontend shows can be tied to the span that caused it. An event published outside any span
carries none.
