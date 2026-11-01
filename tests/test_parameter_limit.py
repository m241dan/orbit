"""The limit of five parameters on every function in ``src`` and ``tests``."""

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).parent.parent
"""The repository's root."""

LIMIT = 5
"""The most parameters a function may take."""

ENTRY = Path("src/orbit/cli")
"""The CLI's package, whose commands the marker may forgive their parameter count."""

MAIN = Path("src/orbit/__main__.py")
"""The ``python -m orbit`` shim, whose functions the marker may forgive too."""

FORGIVES = re.compile(r"#\s*#lizard forgives\(parameter_count\):\s*\S")
"""A comment forgiving a function its parameter count, with a reason after the colon."""

BOUND = (["self"], ["cls"])
"""A method's first parameter, as a list of its one name, when it's bound to its instance or
class and so not counted."""

SIX = "def command(\n    {marker}\n    a, b, c, d, e, f,\n) -> None:\n    pass\n"
"""A six-parameter function whose signature's second line holds ``marker``."""

REASON = "# #lizard forgives(parameter_count): Typer takes each option as a parameter."
"""The marker forgiving a function its parameter count, with its reason."""

type Function = ast.FunctionDef | ast.AsyncFunctionDef
"""A function or method, sync or async."""


def _functions(node: ast.AST, in_class: bool = False) -> Iterator[tuple[Function, bool]]:
    """Every function under ``node``, nested ones included, each with whether it's a method:
    defined in a class's body, as ``in_class`` says ``node``'s children are."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield child, in_class
            yield from _functions(child)
        else:
            yield from _functions(child, in_class or isinstance(child, ast.ClassDef))


def _parameter_count(function: Function, method: bool) -> int:
    """How many parameters ``function`` takes: positional-only, regular and keyword-only ones,
    ``*args`` and ``**kwargs``, but no bare ``*``, nor a ``method``'s ``self`` or ``cls``."""
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    bound = 1 if method and [first.arg for first in positional[:1]] in BOUND else 0
    starred = [star for star in (arguments.vararg, arguments.kwarg) if star is not None]
    return len(positional) - bound + len(arguments.kwonlyargs) + len(starred)


def _signature_lines(function: Function) -> set[int]:
    """The lines of ``function``'s signature, from its ``def`` through its last parameter or its
    return annotation."""
    parts = [*ast.walk(function.args), *([function.returns] if function.returns else [])]
    ends = [part.end_lineno or 0 for part in parts if isinstance(part, ast.arg | ast.expr)]
    return set(range(function.lineno, max([function.lineno, *ends]) + 1))


def _forgiven_lines(source: str) -> set[int]:
    """The lines of ``source`` with a comment forgiving a function its parameter count, with a
    reason."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return {
        token.start[0]
        for token in tokens
        if token.type == tokenize.COMMENT and FORGIVES.match(token.string)
    }


def _may_be_forgiven(relative: Path) -> bool:
    """Whether the module at ``relative`` holds Entry code, a CLI command or the ``python -m``
    shim, whose functions the marker may forgive."""
    return relative.is_relative_to(ENTRY) or relative == MAIN


def _offenders(root: Path, path: Path) -> list[str]:
    """The functions in the module at ``path`` taking more than :data:`LIMIT` parameters, each
    as ``path:line name (n parameters)``, its path relative to ``root``; those an Entry module's
    marker forgives on their signature are left out."""
    source = path.read_text()
    relative = path.relative_to(root)
    forgiven = _forgiven_lines(source) if _may_be_forgiven(relative) else set()
    counted = [
        (function, _parameter_count(function, method))
        for function, method in _functions(ast.parse(source))
    ]
    return [
        f"{relative}:{function.lineno} {function.name} ({count} parameters)"
        for function, count in counted
        if count > LIMIT and not forgiven & _signature_lines(function)
    ]


def _offenders_in(tmp_path: Path, relative: str, source: str) -> list[str]:
    """The functions past the limit in ``source``, written at ``relative`` under ``tmp_path``."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return _offenders(tmp_path, path)


def test_no_function_in_src_or_tests_takes_more_than_five_parameters() -> None:
    paths = sorted(path for top in ("src", "tests") for path in (ROOT / top).rglob("*.py"))
    offenders = [offender for path in paths for offender in _offenders(ROOT, path)]
    assert not offenders, "Past five parameters:\n" + "\n".join(offenders)


def test_a_six_parameter_function_is_reported_with_its_path_line_and_count(tmp_path: Path) -> None:
    source = "import os\n\n\ndef build(a, b, c, d, e, f) -> None:\n    pass\n"
    assert _offenders_in(tmp_path, "src/m.py", source) == ["src/m.py:4 build (6 parameters)"]


def test_a_five_parameter_function_passes(tmp_path: Path) -> None:
    source = "def build(a, b, c, d, e) -> None:\n    pass\n"
    assert _offenders_in(tmp_path, "src/m.py", source) == []


def test_a_method_s_self_or_cls_is_not_counted(tmp_path: Path) -> None:
    source = (
        "class Thing:\n"
        "    def build(self, a, b, c, d, e) -> None:\n"
        "        pass\n"
        "\n"
        "    @classmethod\n"
        "    def make(cls, a, b, c, d, e) -> None:\n"
        "        pass\n"
    )
    assert _offenders_in(tmp_path, "src/m.py", source) == []


def test_a_bare_star_is_not_counted(tmp_path: Path) -> None:
    source = "def build(a, b, *, c, d, e) -> None:\n    pass\n"
    assert _offenders_in(tmp_path, "src/m.py", source) == []


def test_star_args_kwargs_and_positional_only_parameters_are_counted(tmp_path: Path) -> None:
    source = (
        "def gather(a, b, c, d, *args, **kwargs) -> None:\n"
        "    pass\n"
        "\n"
        "def split(a, b, /, c, *, d, e, f) -> None:\n"
        "    pass\n"
    )
    assert _offenders_in(tmp_path, "src/m.py", source) == [
        "src/m.py:1 gather (6 parameters)",
        "src/m.py:4 split (6 parameters)",
    ]


def test_async_nested_and_conditionally_defined_functions_are_checked(tmp_path: Path) -> None:
    source = (
        "async def outer() -> None:\n"
        "    def inner(self, a, b, c, d, e) -> None:\n"
        "        pass\n"
        "\n"
        "class Thing:\n"
        "    if True:\n"
        "        async def run(self, a, b, c, d, e, f) -> None:\n"
        "            pass\n"
    )
    assert _offenders_in(tmp_path, "src/m.py", source) == [
        "src/m.py:2 inner (6 parameters)",
        "src/m.py:7 run (6 parameters)",
    ]


def test_lambdas_are_not_checked(tmp_path: Path) -> None:
    source = "build = lambda a, b, c, d, e, f: None\n"
    assert _offenders_in(tmp_path, "src/m.py", source) == []


def test_a_cli_function_with_the_marker_and_a_reason_passes(tmp_path: Path) -> None:
    source = SIX.format(marker=REASON)
    assert _offenders_in(tmp_path, "src/orbit/cli/command.py", source) == []


def test_the_python_m_shim_may_carry_the_marker(tmp_path: Path) -> None:
    source = f"def main(a, b, c, d, e, f) -> None:  {REASON}\n    pass\n"
    assert _offenders_in(tmp_path, "src/orbit/__main__.py", source) == []


def test_the_marker_without_a_reason_doesn_t_forgive(tmp_path: Path) -> None:
    source = SIX.format(marker="# #lizard forgives(parameter_count):")
    assert _offenders_in(tmp_path, "src/orbit/cli/command.py", source) == [
        "src/orbit/cli/command.py:1 command (6 parameters)"
    ]


def test_the_marker_outside_the_cli_and_the_shim_doesn_t_forgive(tmp_path: Path) -> None:
    source = SIX.format(marker=REASON)
    assert _offenders_in(tmp_path, "src/orbit/agent/command.py", source) == [
        "src/orbit/agent/command.py:1 command (6 parameters)"
    ]


def test_the_marker_in_the_body_doesn_t_forgive(tmp_path: Path) -> None:
    source = f'def command(a, b, c, d, e, f) -> None:\n    """Run."""\n    {REASON}\n'
    assert _offenders_in(tmp_path, "src/orbit/cli/command.py", source) == [
        "src/orbit/cli/command.py:1 command (6 parameters)"
    ]


def test_the_marker_in_a_string_doesn_t_forgive(tmp_path: Path) -> None:
    source = f'def command(a, b, c, d, e, f="{REASON}"):\n    pass\n'
    assert _offenders_in(tmp_path, "src/orbit/cli/command.py", source) == [
        "src/orbit/cli/command.py:1 command (6 parameters)"
    ]
