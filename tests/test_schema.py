"""The protocol's JSON Schema: the committed file matches the models and accepts what the
encoder writes, with room for a peer's additions."""

import json
import runpy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from orbit.protocol import PROTOCOL_VERSION, encode, protocol_schema
from tests.protocol_examples import EXAMPLES

SCHEMA_FILE = Path(__file__).parent.parent / "schema" / "protocol.schema.json"


def committed() -> dict[str, Any]:
    """The schema as committed to the repository."""
    schema: dict[str, Any] = json.loads(SCHEMA_FILE.read_text())
    return schema


def validate(instance: dict[str, Any]) -> None:
    """Raises ``ValidationError`` unless ``instance`` is valid against the committed schema."""
    Draft202012Validator(committed()).validate(instance)


def encoded(index: int) -> dict[str, Any]:
    """The ``index``th example, encoded and parsed back as plain JSON."""
    data: dict[str, Any] = json.loads(encode(EXAMPLES[index]))
    return data


def test_committed_schema_matches_the_models() -> None:
    assert committed() == protocol_schema(), (
        "schema/protocol.schema.json is stale: run `uv run python -m orbit.protocol.schema`"
    )


def test_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(committed())


@pytest.mark.parametrize("index", range(len(EXAMPLES)))
def test_every_encoded_example_validates(index: int) -> None:
    validate(encoded(index))


@pytest.mark.parametrize("index", range(len(EXAMPLES)))
def test_extra_fields_still_validate(index: int) -> None:
    data = encoded(index)
    data["extra"] = 1
    data["payload"]["extra"] = {"nested": True}

    validate(data)


def test_unknown_type_fails_validation() -> None:
    data = encoded(0)
    data["payload"]["type"] = "no_such_message"

    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("field", ["command_id", "payload"])
def test_missing_required_field_fails_validation(field: str) -> None:
    data = encoded(0)
    del data[field]

    with pytest.raises(ValidationError):
        validate(data)


def test_schema_allows_extra_properties_everywhere() -> None:
    assert '"additionalProperties": false' not in SCHEMA_FILE.read_text()


def test_schema_records_the_protocol_version() -> None:
    assert committed()["x-protocol-version"] == PROTOCOL_VERSION


def test_module_writes_the_schema_to_the_given_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "out" / "protocol.schema.json"
    monkeypatch.setattr("sys.argv", ["schema", str(path)])

    runpy.run_module("orbit.protocol.schema", run_name="__main__")

    assert json.loads(path.read_text()) == protocol_schema()
    assert path.read_text().endswith("}\n")


def test_module_writes_the_schema_under_the_current_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["schema"])

    runpy.run_module("orbit.protocol.schema", run_name="__main__")

    path = tmp_path / "schema" / "protocol.schema.json"
    assert json.loads(path.read_text()) == protocol_schema()
    assert path.read_text().endswith("}\n")


def payload_refs(envelope: str) -> list[str]:
    """The ``$defs`` names the ``envelope`` definition's payload may be."""
    payload = committed()["$defs"][envelope]["properties"]["payload"]
    return [option["$ref"].removeprefix("#/$defs/") for option in payload["oneOf"]]


@pytest.mark.parametrize("envelope", ["CommandEnvelope", "EventEnvelope"])
def test_every_message_requires_its_type(envelope: str) -> None:
    defs = committed()["$defs"]
    refs = payload_refs(envelope)

    assert refs
    assert all("type" in defs[name]["required"] for name in refs)


def test_type_is_required_after_the_other_required_fields() -> None:
    assert committed()["$defs"]["AcceptTask"]["required"] == ["task_id", "type"]


def test_nested_models_do_not_require_a_type() -> None:
    assert "required" not in committed()["$defs"]["Settings"]


def test_payload_without_its_type_fails_validation() -> None:
    payload = next(
        example["payload"]
        for example in map(encoded, range(len(EXAMPLES)))
        if example["payload"]["type"] == "cancel_turn"
    )
    del payload["type"]
    schema = {"$defs": committed()["$defs"], "$ref": "#/$defs/CancelTurn"}

    with pytest.raises(ValidationError, match="'type' is a required property"):
        Draft202012Validator(schema).validate(payload)
