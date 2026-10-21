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

RECORDED_ENV = (
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "ENABLE_TOOL_SEARCH",
    "MCP_TOOL_TIMEOUT",
    "ORBIT_AGENT_TOKEN",
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


def main() -> int:
    scenario_path = Path(sys.argv[1])
    scenario = json.loads(scenario_path.read_text())
    if scenario.get("exit_at_once"):
        sys.stderr.write(scenario.get("stderr", ""))
        return int(scenario.get("exit", 0))
    record_call(scenario_path, bool(scenario.get("spawn")))
    for line in scenario.get("stdout", []):
        print(line, flush=True)
    if scenario.get("hang"):
        time.sleep(60)
    sys.stderr.write(scenario.get("stderr", ""))
    return int(scenario.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
