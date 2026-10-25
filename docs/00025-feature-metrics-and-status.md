# Metrics and status requirements

The decisions and plan for phase 5c of milestone 5 (telemetry) of the control-plane design: a small set of metrics, mostly derived from spans; collector settings passed to agents; and `orbit status`, which prints what a running control plane is doing.

## Goal

A backend that can't derive metrics from spans still gets orbit's essential numbers; the Claude Code CLI's own telemetry lands beside orbit's, labelled with its task and agent; and anyone can see at a glance what a control plane is running.

## Metrics

1. Most metrics come from spans: a span processor in `orbit.telemetry` records counters and histograms as spans end. Live gauges are read through a small gauge port in `orbit.tracing` (register a name, unit and a callback giving values with attributes) that the control plane registers callbacks with; a no-op does nothing. Over a full metrics port: every span site gains a second call; over no direct metrics: the design wants them, and gauges can't come from spans.
2. Metrics go wherever traces go: the same `exporter`, endpoint, protocol and headers; the console exporter for `orbit serve` only; the file exporter writing OTLP JSON metrics lines to `<state dir>/telemetry/metrics-*.jsonl` with the same rotation; exported every `[telemetry] metrics_interval_s` (default 60). Over a separate setting: nobody asked for one; over no file metrics: headless servers would lose them.
3. The metrics: `orbit.tasks` (counter, `{task}`; `orbit.task.status`, `orbit.role`, `orbit.workspace.id`; from `orbit.task`); `orbit.verify.step.duration` (histogram, `s`; `orbit.verify.step.name`, `orbit.verify.step.result`; from `orbit.verify.step` spans); `orbit.agent.duration` (histogram, `s`; `orbit.role`, `orbit.runtime`; from `orbit.agent.process`); `gen_ai.client.token.usage` (histogram, `{token}`; `gen_ai.request.model`, `gen_ai.token.type` of `input`, `output`, `cache_creation`, `cache_read`, `orbit.role`; from task, turn and review spans); `orbit.cost` (counter, `USD`; `gen_ai.request.model`, `orbit.role`; the same spans); `orbit.review.suggestions` (counter, `{suggestion}`; `orbit.role`, `orbit.review.outcome` of `made` or `refused`; from `orbit.review`); gauges `orbit.agents.active` (`{agent}`; `orbit.role`, `orbit.workspace.id`), `orbit.queue.depth` (`{task}`, tasks waiting for a coder slot), `orbit.tasks.pending` (`{task}`; `orbit.workspace.id`), `orbit.frontends.connected` (`{connection}`), `orbit.event_log.size` (`By`; `orbit.workspace.id`, the stored log bytes). Over all-`orbit.` names: GenAI dashboards would miss orbit's tokens; over no workspace attributes: no per-workspace view; over queue depth alone: no backlog view; over the database file's size: says little about logs; over per-session sizes: unbounded series.

## Agents

4. With `exporter = "otlp"`, each agent's environment gets `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp`, the endpoint and protocol, and `OTEL_RESOURCE_ATTRIBUTES` with `orbit.task.id` (when it has a task), `orbit.agent.id`, `orbit.role` and `orbit.workspace.id`. The headers are never passed, and README says authenticated collectors won't receive agents' telemetry. With `console` or `file`, agents get nothing. Over passing the headers: an agent running arbitrary commands could leak the collector's key.

## `orbit status`

5. `orbit status` connects like a frontend (discovery, or `--connect`/`--remote` with the same credential flags as `orbit chat`), sends `Hello` and `Watch(("control_plane",))`, reads the Catalog, `AgentsChanged` and `UsageTotals`, prints, and disconnects. Over reading the store: no running agents, same machine only; over an HTTP endpoint: a second API surface with its own auth.
6. Readable text by default: the server's URL and version; each workspace with its sessions (active marked, state, waiting or paused notes); agents by workspace, role and task; usage today and lifetime. `--json` prints the same as one object. Exit codes: 0 connected; 1 no control plane running, saying so; 2 a connection or credential error, with its message. Over text only: scripts parse text; over JSON only: unfriendly in a terminal.

## Docs

7. `docs/telemetry.md` is completed with the metrics (names, kinds, units, attributes, sources) and the agent pass-through; README's **Telemetry** adds metrics, `metrics_interval_s`, what agents receive and the credential limit; README gains `orbit status`.

## How it will be built

1. Write this record.
2. Export metrics derived from spans: the meter provider, `OtlpJsonMetricsFileExporter`, `metrics_interval_s`, `MetricsFromSpans`.
3. Report live gauges: `Gauges` and `NoGauges` in `orbit.tracing`; the control plane's five gauges.
4. Pass collector settings to agents: `agent_telemetry_env`.
5. Add `orbit status`: `status_report`, text and `--json`, exit codes.

## Deferred

A loopback OTLP relay in orbit that forwards agents' telemetry with the headers, so authenticated collectors receive it too.
