"""Stand-in for the ``claude`` CLI, driven by a JSON scenario file.

Usage: python fake_claude.py SCENARIO.json [claude args...]

The scenario is ``{"stdout": [str, ...], "stderr": str, "exit": int, "hang": bool}``.
Each run appends ``{"argv": [...], "stdin": str, "pid": int}`` to ``SCENARIO.json.calls``.
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    scenario_path = Path(sys.argv[1])
    scenario = json.loads(scenario_path.read_text())
    record = {"argv": sys.argv[2:], "stdin": sys.stdin.read(), "pid": os.getpid()}
    with open(f"{scenario_path}.calls", "a") as calls:
        calls.write(json.dumps(record) + "\n")
    for line in scenario.get("stdout", []):
        print(line, flush=True)
    if scenario.get("hang"):
        time.sleep(60)
    sys.stderr.write(scenario.get("stderr", ""))
    return int(scenario.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
