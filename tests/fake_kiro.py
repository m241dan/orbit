"""Stand-in for ``kiro-cli``, driven by a JSON scenario file.

Usage: python fake_kiro.py SCENARIO.json [kiro-cli args...]

The scenario is ``{"fixture": str, "stdout": [str, ...], "stderr": str, "exit": int, "hang": bool,
"spawn": bool}``, each optional. Each run appends ``{"argv": [...], "pid": int, "cwd": str,
"config": {...} | null, "env": {...}}`` to ``SCENARIO.json.calls``: ``config`` is the agent
config in ``~/.kiro/agents`` its ``--agent`` names, as it is while it runs, and ``env`` holds the
variables in ``RECORDED_ENV`` that are set. It then prints the lines of the Kiro fixture
``fixture`` names, if any, then those of ``stdout``, sleeps for a minute if ``hang``, and writes
``stderr`` before exiting with ``exit``. With ``"spawn": true`` it first starts a sleeping child,
like a command the agent ran, and records its pid as ``child``.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "kiro"
RECORDED_ENV = (
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "ORBIT_TEST_KEPT",
)


def agent_config(argv: list[str]) -> Any:
    """The config of the agent ``argv``'s ``--agent`` names, as Kiro would find it; none if it
    names none or there's no such file."""
    if "--agent" not in argv:
        return None
    path = Path.home() / ".kiro" / "agents" / f"{argv[argv.index('--agent') + 1]}.json"
    return json.loads(path.read_text()) if path.exists() else None


def record_call(scenario_path: Path, spawn: bool) -> None:
    """Append this run's call to the scenario's calls, first starting a sleeping child if
    ``spawn``."""
    argv = sys.argv[2:]
    record: dict[str, Any] = {
        "argv": argv,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "config": agent_config(argv),
        "env": {name: os.environ[name] for name in RECORDED_ENV if name in os.environ},
    }
    if spawn:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        record["child"] = child.pid
    with open(f"{scenario_path}.calls", "a") as calls:
        calls.write(json.dumps(record) + "\n")


def lines(scenario: dict[str, Any]) -> list[str]:
    """The lines the scenario prints: its fixture's, then its own."""
    fixture = scenario.get("fixture")
    replayed = (FIXTURES / fixture).read_text().splitlines() if fixture else []
    return [*replayed, *scenario.get("stdout", [])]


def main() -> int:
    scenario_path = Path(sys.argv[1])
    scenario = json.loads(scenario_path.read_text())
    record_call(scenario_path, bool(scenario.get("spawn")))
    for line in lines(scenario):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    if scenario.get("hang"):
        time.sleep(60)
    sys.stderr.write(scenario.get("stderr", ""))
    return int(scenario.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
