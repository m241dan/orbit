# Telemetry pipeline requirements

The decisions and plan for phase 5a of milestone 5 (telemetry) of the control-plane design: a span port shared by orbit's packages, an OpenTelemetry pipeline behind it with OTLP, console and file exporters, and the first wide event, the task span, carried end to end.

## Goal

With telemetry turned on, every task orbit runs arrives at a collector (or a file) as one wide span with everything worth querying about it, and with telemetry off, orbit installs and does nothing extra.

## Scope

1. Milestone 5 ships as three phases: 5a the pipeline and the task span (this record); 5b frontend commands with the TUI's trace context, planner turns, reviews, agent processes with child spans per request and per verification step, and server connections; 5c the metric set, collector settings passed to agents, `orbit status`, and `docs/telemetry.md` and the README section completed. Over two phases: a large first one; over four: `orbit status` and pass-through are small.

## Packages

2. `orbit.tracing`, a new pure-Python package, holds the span port: a `Telemetry` protocol, its span interface and a no-op; importable by `orbit.agent.application`, `orbit.tui` and `orbit.client`. Contracts: it imports nothing of orbit's; `orbit.agent.domain` and `orbit.protocol` never import it. Over the port in the application only: no shared port for the TUI and client; over the port in `orbit.protocol`: the wire contract would hold code that never crosses it, one import from the domain.
3. `orbit.telemetry`, a new package, implements the port with the OpenTelemetry SDK and exporters and is wired only by `orbit.cli` (a contract); `opentelemetry` is imported only there.
4. Trace context crosses the wire in the envelope's existing `traceparent`/`tracestate` fields, the carrier: `orbit.protocol` gains pure getter and setter helpers over them, and `orbit.telemetry` uses OpenTelemetry's W3C propagator through them.
5. `orbit[telemetry]` installs the SDK and the OTLP HTTP exporter; `orbit[telemetry-grpc]` adds the gRPC exporter. Telemetry configured without its extra is a startup error (status 2) naming the extra. Over one extra: every user installs `grpcio`; over HTTP only: some setups expect gRPC.

## Configuration

6. `[telemetry]` in the user config: `exporter` (`"none"` default, `"otlp"`, `"console"`, `"file"`), `endpoint`, `protocol` (`"http/protobuf"` default or `"grpc"`), `headers_file`, `file_max_mb` (default 50), `file_keep` (default 5), `service_name`. The standard `OTEL_EXPORTER_OTLP_*` variables override the endpoint and headers. Over environment only: the design puts telemetry in config; over no environment overrides: container setups ignored.
7. `headers_file`: one `Name: value` per line, ignoring blank lines and `#` comments; it must be owner-only, refused with the `chmod 600` hint otherwise. Over TOML: a second syntax for the secret file; over `k=v,…`: awkward with commas.
8. Resource attributes: `service.name` (`"orbit"`, or `service_name`), `service.version`, `service.namespace` (`"orbit-serve"` or `"orbit-chat"`), `service.instance.id` (new on each start), `host.name`. Over a persisted instance id: the OpenTelemetry default was chosen; over the name alone: local and served orbits indistinguishable.

## Exporting

9. The file exporter writes OTLP JSON, one `ExportTraceServiceRequest` per line (what the Collector's `otlpjsonfile` receiver reads), to `<state dir>/telemetry/spans-*.jsonl`, rotated by `file_max_mb` and `file_keep`. Over console JSON: no collector reads it; over a configurable path: the state directory is the home.
10. The console exporter writes to stderr and is for `orbit serve` only: with `orbit chat`, `exporter = "console"` is a startup error (status 2) pointing to `"file"`. Over holding console spans until the TUI exits: a debugging aid that shows nothing while it matters; over redirecting them to a file: the file exporter already does that.
11. On exit, orbit flushes for at most 5 seconds, then drops the rest with one warning naming the count; `orbit serve`'s graceful shutdown includes it. Over the exporter's timeouts: exits could take a minute; over no flush: the last task's span is lost.
12. Plain warnings (an unreachable collector, a dropped flush, config problems) go to stderr through stdlib `logging` at WARNING and above. While the TUI runs they're captured and shown as a transcript note, once per kind per run (the kind being the logger name and message template), and printed to stderr after the TUI exits. Over a file: nobody looks there; over dropping them: silent failure; over showing again after a recovery: libraries rarely log recovery.
13. No sampling: every span is kept.

## The task span

14. Spans are found implicitly: the port keeps the current span in a `ContextVar`, so code anywhere inside an async call tree enriches it and child spans nest on their own. Over passing spans explicitly: signature changes across tested code.
15. Each task has a parent span `orbit.task`, from when it's ready to run until its status is final (after merging), carrying the wide attributes: `orbit.workspace.id`, `orbit.session.id`, `orbit.task.id`, `orbit.task.title`, `orbit.role`, `orbit.runtime` (`claude`), `gen_ai.request.model`, `orbit.trigger` (today's origin), `orbit.task.fixes` for a fix task, `orbit.task.fix_attempts`, `orbit.task.wait_ms`, `orbit.task.check_runs`, tokens as `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.cache_read.input_tokens`, `orbit.cost.usd`, `orbit.verify.result`, `orbit.verify.failed_step`, `orbit.merge.result`, `orbit.task.status` and `orbit.task.reason`. Two timing-only children: `orbit.task.wait` (only when it waited, with `orbit.wait.position` and `orbit.wait.slots_busy`) and `orbit.task.run` (the agent's start to the end). Each task's span starts its own trace in 5a. Over one run span with the wait as an attribute: the wait invisible on a timeline; over two sibling spans: nothing ties them as one task.
16. Each verification step is a span event `orbit.verify.step` on `orbit.task`, with its name, result and duration. Over parallel list attributes: fragile alignment; over child spans now: they'd duplicate 5b's.
17. Errors set the span's status and are recorded as an exception event with the message; a give-up sets `orbit.task.reason` and adds the event `orbit.task.gave_up`; a merge conflict adds `orbit.merge.conflict`. No prompt bodies, agent messages, diffs or outputs on spans.
18. Names are low-cardinality and `orbit.`-prefixed: `orbit.task`, `orbit.task.wait`, `orbit.task.run`, later `orbit.planner.turn`, `orbit.review`, `orbit.agent.process`, `orbit.command`, `orbit.connection`, `orbit.agent.request`, `orbit.verify.step`. Over human names: no grouping; over unprefixed names: collisions in shared backends.

## Docs

19. `docs/telemetry.md` starts in 5a with the `orbit.task` spans' names, attributes and events; README gains a **Telemetry** section: enabling it, the extras, `[telemetry]`, the console and file exporters, and what is and isn't recorded. 5b and 5c extend both.

## How it will be built

1. Write this record.
2. Add the tracing port: `orbit.tracing` with `Telemetry`, `Span`, `current_span`, `NoTelemetry`; `trace_carrier` and `with_trace_carrier` in `orbit.protocol`; contracts.
3. Export spans with the OpenTelemetry SDK: `orbit.telemetry` with `OtelTelemetry`, `TelemetrySettings`, `OtlpJsonFileExporter`; the extras; startup, flush and logging; wiring in `orbit.cli`; README's **Telemetry**.
4. Show telemetry warnings in the TUI: `WarningCapture`.
5. Trace each task: the `orbit.task` span, its children and events; `docs/telemetry.md`.
