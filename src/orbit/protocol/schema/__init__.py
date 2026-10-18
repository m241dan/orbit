"""The protocol's JSON Schema, which says what every envelope looks like on the wire.

Run as ``python -m orbit.protocol.schema [path]`` to write it, by default to
``schema/protocol.schema.json``.
"""

import json
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from orbit.protocol.envelope import CommandEnvelope, EventEnvelope
from orbit.protocol.messages import PROTOCOL_VERSION

SCHEMA_PATH = Path("schema/protocol.schema.json")
"""Where the schema is written, relative to the repository root."""


def protocol_schema() -> dict[str, Any]:
    """One JSON Schema document accepting a command envelope or an event envelope, with every
    model under ``$defs``; its objects allow extra properties, as decoding does."""
    refs, schema = models_json_schema(
        [(CommandEnvelope, "validation"), (EventEnvelope, "validation")],
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Orbit protocol",
        "x-protocol-version": PROTOCOL_VERSION,
        "oneOf": list(refs.values()),
        "$defs": _open(schema["$defs"]),
    }


def _open(node: Any) -> Any:
    """``node`` with every ``"additionalProperties": false`` left out."""
    if isinstance(node, list):
        return [_open(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {key: _open(value) for key, value in node.items() if not _closes(key, value)}


def _closes(key: str, value: Any) -> bool:
    """Whether ``key`` and ``value`` forbid properties a schema doesn't declare."""
    return key == "additionalProperties" and value is False


def write_schema(path: Path) -> None:
    """Writes the protocol's schema to ``path`` as sorted, indented JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(protocol_schema(), indent=2, sort_keys=True) + "\n")
