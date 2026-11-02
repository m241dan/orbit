import ast
import pkgutil
import re
import tomllib
from pathlib import Path
from typing import Any

import orbit
import orbit.tui

TESTS = Path(__file__).parent
PYPROJECT = TESTS.parent / "pyproject.toml"
VIEW_CONTRACT = "TUI view is independent of Textual"
TRACING_CONTRACT = "Tracing imports no orbit package"
EXECUTION_CONTRACT = "Execution imports no orbit package"


def _contract(name: str) -> dict[str, Any]:
    """The import-linter contract called ``name`` in pyproject.toml."""
    config = tomllib.loads(PYPROJECT.read_text())
    contracts: list[dict[str, Any]] = config["tool"]["importlinter"]["contracts"]
    return next(contract for contract in contracts if contract["name"] == name)


def _widget_modules() -> list[str]:
    """The frontend's modules other than the package itself and its view."""
    modules = pkgutil.walk_packages(orbit.tui.__path__, prefix="orbit.tui.")
    return [
        module.name
        for module in modules
        if module.name != "orbit.tui.view" and not module.name.startswith("orbit.tui.view.")
    ]


def _forbids(patterns: list[str], module: str) -> bool:
    """Whether one of import-linter's ``patterns`` names ``module``: ``*`` stands for one part
    of a module's name and ``**`` for one or more."""
    return any(re.fullmatch(_pattern_regex(pattern), module) for pattern in patterns)


def _pattern_regex(pattern: str) -> str:
    """A regular expression for the module names that the import-linter ``pattern`` names."""
    parts = (
        {"*": r"[^.]+", "**": r"[^.]+(\.[^.]+)*"}.get(part, re.escape(part))
        for part in pattern.split(".")
    )
    return r"\.".join(parts)


def test_the_view_may_import_no_widget_module() -> None:
    forbidden: list[str] = _contract(VIEW_CONTRACT)["forbidden_modules"]
    missing = [module for module in _widget_modules() if not _forbids(forbidden, module)]
    assert not missing, f"{VIEW_CONTRACT!r} doesn't forbid {', '.join(missing)}"


def test_contract_patterns_name_modules_as_import_linter_does() -> None:
    assert not _forbids(["textual", "orbit.tui.app"], "orbit.tui.sessions")
    assert _forbids(["orbit.tui.sessions"], "orbit.tui.sessions")
    assert _forbids(["orbit.tui.*"], "orbit.tui.sessions")
    assert not _forbids(["orbit.tui.*"], "orbit.tui.a.b")
    assert _forbids(["orbit.tui.**"], "orbit.tui.a.b")


def _imported(node: ast.AST) -> list[str]:
    """What ``node`` imports, as dotted names, if it's an import."""
    if isinstance(node, ast.ImportFrom):
        return [f"{node.module}.{alias.name}" for alias in node.names]
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return []


def _test_module_imports(source: str) -> list[str]:
    """What the Python ``source`` imports from test modules, as dotted names."""
    imported = [name for node in ast.walk(ast.parse(source)) for name in _imported(node)]
    return [name for name in imported if name.startswith("tests.test_")]


def test_no_test_module_imports_from_another() -> None:
    importing = [
        path.name for path in sorted(TESTS.glob("*.py")) if _test_module_imports(path.read_text())
    ]
    assert not importing, (
        f"{', '.join(importing)} import from test modules; share helpers through helper modules"
    )


def test_imports_from_test_modules_are_found_however_they_are_written() -> None:
    source = (
        "import os\n"
        "import tests.test_tui\n"
        "from tests import test_cli, cli_fakes\n"
        "from tests.test_replay import notice\n"
        "from tests.agent_fakes import notice\n"
    )
    assert _test_module_imports(source) == [
        "tests.test_tui",
        "tests.test_cli",
        "tests.test_replay.notice",
    ]


FIXED_WAIT_REASON = "# fixed wait:"
"""What a line that sleeps for a set while must carry, followed by why it can't wait on a
condition instead."""


def _sleep_seconds(call: ast.Call) -> object:
    """What ``call`` passes ``asyncio.sleep`` or ``time.sleep`` as its seconds, if it calls one
    of them with a literal."""
    match call:
        case ast.Call(
            func=ast.Attribute(value=ast.Name(id="asyncio" | "time"), attr="sleep"),
            args=[ast.Constant(value=seconds), *_],
        ):
            return seconds
    return None


def _is_positive_number(value: object) -> bool:
    """Whether ``value`` is a number, but not a bool, above zero."""
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _unexplained_fixed_sleeps(source: str) -> list[int]:
    """The lines of the Python ``source`` that sleep for a positive number of seconds written
    out as a literal, without saying why."""
    lines = source.splitlines()
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    return sorted(
        call.lineno
        for call in calls
        if _is_positive_number(_sleep_seconds(call))
        and FIXED_WAIT_REASON not in lines[call.lineno - 1]
    )


def test_no_test_module_sleeps_for_a_fixed_while_without_saying_why() -> None:
    sleeping = [
        f"{path.name}:{line}"
        for path in sorted(TESTS.glob("test_*.py"))
        for line in _unexplained_fixed_sleeps(path.read_text())
    ]
    assert not sleeping, (
        f"{', '.join(sleeping)} sleep for a fixed while; wait on a condition with "
        f"tests/waiting.py or tests/loop_quiet.py, or say why with {FIXED_WAIT_REASON!r}"
    )


def test_fixed_sleeps_are_found_however_they_are_written() -> None:
    source = (
        "import asyncio, time\n"
        "async def waits() -> None:\n"
        "    await asyncio.sleep(0.05)\n"
        "    time.sleep(0.02)\n"
        "    time.sleep(1)\n"
        "    time.sleep(True)\n"
        "    await asyncio.sleep(0.05)  # fixed wait: a sampling interval\n"
        "    await asyncio.sleep(0)\n"
        "    await asyncio.sleep(poll)\n"
        "    await clock.sleep(5)\n"
        "    sleep(5)\n"
        "    print('time.sleep(60)')\n"
    )
    assert _unexplained_fixed_sleeps(source) == [3, 4, 5]


def test_tracing_imports_no_orbit_package() -> None:
    forbidden: list[str] = _contract(TRACING_CONTRACT)["forbidden_modules"]
    modules = pkgutil.iter_modules(orbit.__path__, prefix="orbit.")
    others = sorted(module.name for module in modules if module.name != "orbit.tracing")
    missing = [module for module in others if not _forbids(forbidden, module)]
    assert not missing, f"{TRACING_CONTRACT!r} doesn't forbid {', '.join(missing)}"


def test_neither_the_domain_nor_the_protocol_may_import_tracing() -> None:
    contract = _contract("Neither the domain nor the protocol traces")
    assert contract["type"] == "forbidden"
    assert set(contract["source_modules"]) == {"orbit.agent.domain", "orbit.protocol"}
    assert _forbids(contract["forbidden_modules"], "orbit.tracing")


def _top_level() -> set[str]:
    """Orbit's top-level packages and modules, but the ``python -m orbit`` shim."""
    modules = pkgutil.iter_modules(orbit.__path__, prefix="orbit.")
    return {module.name for module in modules} - {"orbit.__main__"}


def test_execution_imports_no_orbit_package() -> None:
    contract = _contract(EXECUTION_CONTRACT)
    assert contract["source_modules"] == ["orbit.execution"]
    others = sorted(_top_level() - {"orbit.execution"})
    missing = [module for module in others if not _forbids(contract["forbidden_modules"], module)]
    assert not missing, f"{EXECUTION_CONTRACT!r} doesn't forbid {', '.join(missing)}"


def test_neither_the_domain_nor_the_protocol_may_import_execution() -> None:
    contract = _contract("Neither the domain nor the protocol uses execution")
    assert contract["type"] == "forbidden"
    assert set(contract["source_modules"]) == {"orbit.agent.domain", "orbit.protocol"}
    assert _forbids(contract["forbidden_modules"], "orbit.execution")


def test_only_the_cli_may_import_telemetry() -> None:
    contract = _contract("Only the CLI uses telemetry")
    assert _forbids(contract["forbidden_modules"], "orbit.telemetry")
    assert _top_level() - set(contract["source_modules"]) == {"orbit.cli", "orbit.telemetry"}


def test_only_telemetry_may_import_opentelemetry() -> None:
    contract = _contract("Only telemetry uses OpenTelemetry")
    assert contract["forbidden_modules"] == ["opentelemetry"]
    assert _top_level() - set(contract["source_modules"]) == {"orbit.telemetry"}
