"""The error a project file or an agent's proposal raises when it's invalid, and the readable
description of what's wrong with it."""

from pydantic import ValidationError

INVALID_CONFIG = "Invalid orbit configuration: "
"""What the message about an invalid configuration starts with."""


class ConfigError(ValueError):
    """The configuration is invalid; the message says where and why, on one line per problem."""


def validation_problems(error: ValidationError) -> str:
    """``error``'s problems, one per line, each saying where and why."""
    problems = []
    for problem in error.errors():
        where = ".".join(str(part) for part in problem["loc"])
        problems.append(f"{where}: {problem['msg']}")
    return "\n".join(problems)
