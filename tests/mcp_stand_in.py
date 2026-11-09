"""A tiny MCP server over stdio, standing in for the external servers orbit proxies.

It speaks newline-delimited JSON-RPC on stdin and stdout, offering its tools two to a page:
``get_issue`` (an issue's text), ``get issue!`` (the same, under a name agents can't have),
``whoami`` (its process id, directory and the environment it started with, as JSON), ``oops``
(the tool's own error), ``slow`` (never answers) and ``fail`` (exits with status 3 without
answering), and, unlisted,
``picture`` (text and an image), ``hang up`` (closes its stdout and waits) and ``malformed`` (a
result that isn't one).

Its first argument, if any, is its mode: ``hang-up`` closes its stdout before reading anything,
``refuse-list`` answers ``tools/list`` with an error, ``malformed-list`` answers it with a tool
whose input schema isn't an object, ``garbage`` writes a line that isn't a
message first, and ``silent`` writes its process id to the file its second argument names and
never answers.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
PROTOCOL_VERSION = "2025-06-18"
PAGE_SIZE = 2
OBJECT = {"type": "object", "properties": {"number": {"type": "integer"}}}
TOOLS = [
    {"name": "get_issue", "description": "Get an issue.", "inputSchema": OBJECT},
    {"name": "get issue!", "description": "Get an issue, oddly named.", "inputSchema": OBJECT},
    {"name": "whoami", "inputSchema": {"type": "object"}},
    {"name": "oops", "inputSchema": {"type": "object"}},
    {"name": "slow", "inputSchema": {"type": "object"}},
    {"name": "fail", "inputSchema": {"type": "object"}},
]


def send(message: dict[str, Any]) -> None:
    """Write ``message`` as one line of JSON."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def environment() -> dict[str, str]:
    """The environment the process was started with, before Python's locale coercion added to
    it."""
    entries = Path("/proc/self/environ").read_bytes().decode().split("\0")
    return dict(entry.split("=", 1) for entry in entries if entry)


def text(value: str, error: bool = False) -> dict[str, Any]:
    """A tool's result holding ``value`` as text, an error if ``error``."""
    return {"content": [{"type": "text", "text": value}], "isError": error}


def listed(params: dict[str, Any]) -> dict[str, Any]:
    """The page of tools ``params``' cursor asks for; a tool whose input schema isn't an object
    if the mode is ``malformed-list``."""
    if MODE == "malformed-list":
        return {"tools": [{"name": "broken", "inputSchema": "not an object"}]}
    start = int(params.get("cursor") or 0)
    page = TOOLS[start : start + PAGE_SIZE]
    following = start + PAGE_SIZE
    return {"tools": page, **({"nextCursor": str(following)} if following < len(TOOLS) else {})}


def issue(arguments: dict[str, Any]) -> dict[str, Any]:
    """An issue's text."""
    return text(f"Issue {arguments.get('number')}: it's broken.")


def whoami(arguments: dict[str, Any]) -> dict[str, Any]:
    """Its process id, directory and the environment it started with, as JSON."""
    return text(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "env": environment()}))


def oops(arguments: dict[str, Any]) -> dict[str, Any]:
    """The tool's own error."""
    return text("it went wrong", error=True)


def slow(arguments: dict[str, Any]) -> dict[str, Any]:
    """Nothing, ever."""
    time.sleep(3600)
    return text("too late")


def fail(arguments: dict[str, Any]) -> dict[str, Any]:
    """Nothing: it exits with status 3."""
    sys.exit(3)


def picture(arguments: dict[str, Any]) -> dict[str, Any]:
    """Text and an image."""
    image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}
    return {"content": [{"type": "text", "text": "A picture:"}, image]}


def malformed(arguments: dict[str, Any]) -> dict[str, Any]:
    """A result whose content isn't a list."""
    return {"content": "not a list"}


def hang_up(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Nothing: it closes stdout and waits, never to answer again."""
    sys.stdout.close()
    os.close(1)
    time.sleep(3600)
    return text("too late")


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "get_issue": issue,
    "get issue!": issue,
    "whoami": whoami,
    "oops": oops,
    "slow": slow,
    "fail": fail,
    "picture": picture,
    "hang up": hang_up,
    "malformed": malformed,
}


def called(params: dict[str, Any]) -> dict[str, Any] | None:
    """The result of the tool call ``params`` give; none if there's no such tool."""
    handler = HANDLERS.get(params["name"])
    return None if handler is None else handler(params.get("arguments") or {})


def answer(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """The result of the request ``method`` with ``params``; none if there's no such method."""
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stand-in", "version": "1"},
        }
    if method == "tools/list":
        return None if MODE == "refuse-list" else listed(params)
    if method == "tools/call":
        return called(params)
    if method == "ping":
        return {}
    return None


def begin() -> None:
    """Behave as the mode says before reading anything."""
    if MODE == "hang-up":
        hang_up()
    if MODE == "silent":
        write_pid(Path(sys.argv[2]))
        time.sleep(3600)
    if MODE == "garbage":
        print("this isn't a message", flush=True)


def write_pid(path: Path) -> None:
    """Write this process's pid to ``path`` whole, through a file renamed over it, so a reader
    waiting for it to exist never finds it empty."""
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(str(os.getpid()))
    partial.replace(path)


def reply(line: str) -> None:
    """Answer the request ``line`` holds, unless it's a notification."""
    message = json.loads(line)
    if "id" not in message:
        return
    result = answer(message["method"], message.get("params") or {})
    if result is None:
        error = {"code": -32602, "message": f"no such method or tool in {line.strip()}"}
        send({"jsonrpc": "2.0", "id": message["id"], "error": error})
    else:
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})


def main() -> None:
    begin()
    for line in sys.stdin:
        reply(line)


if __name__ == "__main__":
    main()
