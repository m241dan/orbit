"""The error a project file or an agent's proposal raises when it's invalid, and the readable
description of what's wrong with it."""

from pydantic import ValidationError

INVALID_CONFIG = "Invalid orbit configuration: "
"""What the message about an invalid configuration starts with."""


KEY_PROBLEM = "[key]"
"""What pydantic puts after a table's key in the location of a problem with the key itself."""


class ConfigError(ValueError):
    """The configuration is invalid; the message says where and why, on one line per problem."""


def validation_problems(error: ValidationError) -> str:
    """``error``'s problems, one per line, each saying where and why. A default left unworked
    out because another field is invalid isn't a problem of its own."""
    problems = []
    for problem in error.errors():
        if problem["type"] == "default_factory_not_called":
            continue
        problems.append(f"{dotted(problem['loc'])}: {problem['msg']}")
    return "\n".join(problems)


def dotted(loc: tuple[int | str, ...]) -> str:
    """The dotted key of ``loc``, the location of a problem, such as ``roles.coder.model``; a
    problem with a table's key is at that key."""
    parts = loc[:-1] if loc[-1:] == (KEY_PROBLEM,) else loc
    return ".".join(str(part) for part in parts)
