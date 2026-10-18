# Wire contract requirements

The decisions and plan for phase 1d of milestone 1 (generic protocol) of the control-plane design: the JSON wire encoding with tolerant decoding, the generated JSON Schema with its drift test, and the hand-written protocol document.

## Goal

A frontend in any language can build against a file: the schema says what every message looks like on the wire, `docs/protocol.md` says how the conversation goes, and orbit's own encoder and decoder follow the compatibility rule (additive changes never break an older peer). Nothing sends JSON yet; the server milestone does.

## Wire encoding

1. `encode`, `decode_command` and `decode_event` in `orbit.protocol` turn the envelopes into JSON text and back: one message per frame, the existing `type` discriminator, no network dependencies.
2. Decoding is tolerant through a before-validator on `_Message` and the envelope base that drops unknown keys only under a `wire` validation context; in-process construction stays strict. Over `extra="ignore"` everywhere: in-process code and tests would lose typo-catching; over parallel tolerant models: every message defined twice.
3. `decode_event` returns `None` for an unknown event type, to be skipped; `decode_command` returns the envelope; both raise `ProtocolError` for malformed JSON or an invalid known message, carrying the `command_id` when it could be read, and an unknown command type is a `ProtocolError` marked `unknown`. Over everything raising: clients must catch to skip, and the server couldn't tell unknown from garbage; over a result union: heavier for the common case.
4. A new value in an existing `Literal` is a meaning change needing a major version bump; additive change happens through new fields or new event types. Over skipping events that fail validation: state silently lost; over `str` on the wire: every client handles any string and the schema loses its value lists.
5. `encode` writes every field, nulls and defaults included, as compact JSON. Over leaving out `None`: required-but-nullable fields such as `Snapshot.review` would break; over leaving out defaults: clients must know every default.
6. `ThreadedSession` keeps passing objects. Over JSON across the thread: a cost on every event for proof the round-trip tests give; over a debug flag: one more mode to test.

## Schema

7. `schema/protocol.schema.json` at the repository root: one file whose top level offers `CommandEnvelope` and `EventEnvelope`, every message under `$defs`, carrying `PROTOCOL_VERSION`. Over `docs/`: unconventional for a generated artefact; over inside the package: buried, and serving can generate at runtime; over a versioned directory: only one major exists.
8. The schema describes the wire: objects allow extra properties; required fields, `Literal` value lists and the `type` discriminator are kept. Over pydantic's strict output: clients validating against it would reject additive changes; over leaving out `required`: it promises little.
9. `protocol_schema() -> dict` in `orbit.protocol` builds it; `uv run python -m orbit.protocol.schema` writes the file; a drift test fails when the file differs from the models, naming that command. Over a hidden `orbit schema` command: a developer tool in the user's CLI; over a test that rewrites the file: surprising.

## Tests

10. A table of one example per message type (`EXAMPLES` in `tests/protocol_examples.py`), with a test that it covers every member of `Command` and `Event`; each encodes, decodes to an equal value, and validates against the schema file with `jsonschema`, a new dev dependency. Over `hypothesis`: heavier, failures harder to read; over not checking the schema: schema and encoder could disagree unnoticed.

## Docs

11. `docs/protocol.md`, hand-written: the envelope, the handshake, command outcomes, sequencing and reconnect, compatibility with the `Literal` rule, sessions and workspaces, the schema and how to regenerate it, and a table of every code.
12. The codes are gathered as `REFUSAL_CODES` and `ERROR_CODES` in `orbit.protocol`, and a test checks each appears in the doc's table. Over no test: the table drifts; over generating it: a half-generated doc.
13. README's Architecture section gains a line pointing to `docs/protocol.md` and the schema.

## How it will be built

1. Write this record.
2. Add the JSON wire encoding: the `wire`-context before-validator; `encode`, `decode_command`, `decode_event` and `ProtocolError` in a new `orbit.protocol.wire`; `EXAMPLES` in `tests/protocol_examples.py` covering both unions, each round-tripping.
3. Generate the protocol's JSON Schema: `protocol_schema()` in a new `orbit.protocol.schema`, runnable as `python -m orbit.protocol.schema` to write `schema/protocol.schema.json`; the drift test; every entry in `EXAMPLES` validated against the file with `jsonschema`.
4. Document the protocol: `docs/protocol.md`, `REFUSAL_CODES` and `ERROR_CODES` with the doc test, and the README line.

## Out of scope

Using the encoding over a network, and refusing undecodable frames (server milestone); a versioned schema directory (with a second major version).
