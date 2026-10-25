"""Stand-in for the ``claude`` CLI, driven by a JSON scenario file.

Usage: python fake_claude.py SCENARIO.json [claude args...]

The scenario is ``{"stdout": [str, ...], "stderr": str, "exit": int, "hang": bool, "spawn": bool}``.
With ``"exit_at_once": true`` it writes ``stderr`` and exits with ``exit`` before reading its
stdin or recording the call, like a CLI rejecting its arguments. Otherwise each run appends
``{"argv": [...], "stdin": str, "pid": int, "env": {...}, "cwd": str}`` to ``SCENARIO.json.calls``;
``env`` holds the variables in ``RECORDED_ENV`` that are set. With ``"spawn": true`` it first
starts a sleeping child, like a command the agent ran, and records its pid as ``child``.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

RECORDED_ENV = (
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "ENABLE_TOOL_SEARCH",
    "MCP_TOOL_TIMEOUT",
    "ORBIT_AGENT_TOKEN",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_LOGS_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_RESOURCE_ATTRIBUTES",
)


def record_call(scenario_path: Path, spawn: bool) -> None:
    """Append this run's call to the scenario's calls, first starting a sleeping child if
    ``spawn``."""
    env = {name: os.environ[name] for name in RECORDED_ENV if name in os.environ}
    record = {
        "argv": sys.argv[2:],
        "stdin": sys.stdin.read(),
        "pid": os.getpid(),
        "env": env,
        "cwd": os.getcwd(),
    }
    if spawn:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        record["child"] = child.pid
    with open(f"{scenario_path}.calls", "a") as calls:
        calls.write(json.dumps(record) + "\n")


def write_raw(stream: TextIO, text: str) -> None:
    """Write ``text`` to ``stream`` as bytes, each lone surrogate ``\\udcXX`` as the byte
    ``XX``, so a scenario can print invalid UTF-8."""
    stream.buffer.write(text.encode("utf-8", "surrogateescape"))
    stream.flush()


def main() -> int:
    scenario_path = Path(sys.argv[1])
    scenario = json.loads(scenario_path.read_text())
    if scenario.get("exit_at_once"):
        write_raw(sys.stderr, scenario.get("stderr", ""))
        return int(scenario.get("exit", 0))
    record_call(scenario_path, bool(scenario.get("spawn")))
    for line in scenario.get("stdout", []):
        write_raw(sys.stdout, line + "\n")
    if scenario.get("hang"):
        time.sleep(60)
    write_raw(sys.stderr, scenario.get("stderr", ""))
    return int(scenario.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
