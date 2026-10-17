import pkgutil
import re
import tomllib
from pathlib import Path
from typing import Any

import orbit.tui

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"
VIEW_CONTRACT = "TUI view is independent of Textual"


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
