"""The Kiro fixtures, captured from kiro-cli by scripts/capture_kiro_fixtures.py: what they hold
for the tests that use them, and nothing of the machine or token they were captured with."""

import importlib.util
import json
import re
import secrets
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

KIRO = Path(__file__).parent / "fixtures" / "kiro"
CAPTURE_SCRIPT = Path(__file__).parent.parent / "scripts" / "capture_kiro_fixtures.py"
FIXTURES = sorted(KIRO.glob("*.jsonl"))

# A home other than the placeholder's, or a token: after ``Bearer`` anything but the placeholder,
# or anything as long as the capture's tokens.
LEAKS = re.compile(r"/home/(?!user\b)|Bearer (?!TOKEN\b)|[\w-]{43,}")


def capture_script() -> ModuleType:
    """The capture script, loaded as a module."""
    spec = importlib.util.spec_from_file_location("capture_kiro_fixtures", CAPTURE_SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def events(fixture: Path) -> list[dict[str, Any]]:
    """The events in ``fixture``, one per line."""
    return [json.loads(line) for line in fixture.read_text().splitlines()]


def orbit_tool_calls(fixture: Path) -> list[str]:
    """The orbit tools ``fixture``'s agent called through orbit's MCP server, in order."""
    updates = [event["data"]["update"] for event in events(fixture) if "update" in event["data"]]
    return [
        update["_meta"]["kiro"]["toolName"]
        for update in updates
        if update["sessionUpdate"] == "tool_call"
        and update["_meta"]["kiro"].get("mcpServerName") == "orbit"
    ]


def test_every_run_is_captured() -> None:
    names = {fixture.name for fixture in FIXTURES}
    assert names == {
        "plain.jsonl",
        "orbit-tools.jsonl",
        "outage.jsonl",
        "tools.jsonl",
        "bad-model.jsonl",
    }


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_a_fixture_is_json_lines_of_typed_events(fixture: Path) -> None:
    assert all(isinstance(event["type"], str) for event in events(fixture))


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_a_fixture_holds_no_home_path_or_token(fixture: Path) -> None:
    assert LEAKS.search(fixture.read_text()) is None


def test_the_leak_check_catches_a_home_path_and_a_token() -> None:
    assert LEAKS.search('{"path":"/home/korisd/orbit"}')
    assert LEAKS.search('{"Authorization":"Bearer abc123"}')
    assert LEAKS.search(f'{{"text":"{secrets.token_urlsafe(32)}"}}')
    assert not LEAKS.search('{"path":"/home/user/orbit","Authorization":"Bearer TOKEN"}')


def test_the_agent_calls_orbits_tools_through_its_mcp_server() -> None:
    calls = orbit_tool_calls(KIRO / "orbit-tools.jsonl")
    assert calls == ["get_task", "report_progress", "complete_task"]


def test_an_outage_ends_in_a_run_error() -> None:
    assert events(KIRO / "outage.jsonl")[-1]["type"] == "runError"


def test_each_captured_run_has_its_exit_code() -> None:
    exit_codes = json.loads((KIRO / "exit-codes.json").read_text())
    assert exit_codes == {"plain.jsonl": 0, "orbit-tools.jsonl": 0, "outage.jsonl": 1}


def test_the_capture_script_runs_exactly_the_captured_fixtures() -> None:
    script = capture_script()
    assert {f"{run.name}.jsonl" for run in script.RUNS} == {fixture.name for fixture in FIXTURES}


def test_a_bad_model_ends_in_a_run_error_naming_it() -> None:
    last = events(KIRO / "bad-model.jsonl")[-1]
    assert last["type"] == "runError"
    assert "no-such-model" in last["data"]["message"]


def anonymised(output: str) -> str:
    """``output`` as the capture script anonymises it, with token ``secret`` and home
    ``/home/kiro``."""
    result: str = capture_script().anonymised(output, "secret", "/home/kiro")
    return result


def test_anonymising_keeps_lines_that_are_not_json() -> None:
    output = '{"type": "a"}\nWarning: slow\n\n{"type": "b"}\n'
    assert anonymised(output) == output


def test_anonymising_replaces_the_home_and_token_on_every_line() -> None:
    output = '{"path": "/home/kiro/x", "auth": "Bearer secret"}\nWarning: /home/kiro secret\n'
    assert anonymised(output) == (
        '{"path": "/home/user/x", "auth": "Bearer TOKEN"}\nWarning: /home/user TOKEN\n'
    )


def test_anonymising_numbers_session_ids_in_order_of_first_appearance() -> None:
    output = '{"sessionId": "s-one"}\n{"data": {"sessionId": "s-two"}}\n{"sessionId": "s-one"}\n'
    assert anonymised(output) == (
        '{"sessionId": "00000000-0000-0000-0000-000000000001"}\n'
        '{"data": {"sessionId": "00000000-0000-0000-0000-000000000002"}}\n'
        '{"sessionId": "00000000-0000-0000-0000-000000000001"}\n'
    )


def test_anonymising_replaces_request_ids() -> None:
    output = '{"requestId": "12345678-abcd-4def-8abc-1234567890ab"}\n'
    assert anonymised(output) == '{"requestId": "REQUEST"}\n'
