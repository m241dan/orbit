# Telemetry and logs

- Record one wide span per unit of work: a command, a task run, a request. Put on it what
  explains the outcome: ids, titles, reasons, counts.
- Put errors on the span of the work they ended.
- Write plain logs, to stderr, only for events about the process itself: start, stop, crash.
- Domain and the work inside a unit return values; the unit's span records the outcome.
- Record ids, not content: transcripts, payloads and personal data stay out of spans.
- Telemetry goes through a port in Application, with the SDK in External, so tests run without
  it.
