"""The numbered requirements records tasks name, such as ``docs/00024-feature-metrics.md``, and
moving one whose number another record took by the time its task runs."""

import re
from collections.abc import Collection, Mapping

# A relative path whose file name is a five-digit number, a dash and a name, ending in .md.
_RECORD = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*)(\d{5})-([\w.-]*?)\.md(?![\w-])")
_NUMBERED = re.compile(r"(\d{5})-")
_WIDTH = 5


def record_directories(text: str) -> tuple[str, ...]:
    """The directories of the records ``text`` names, each once in the order first named, ``""``
    for the top level."""
    return tuple(dict.fromkeys(match[1].rstrip("/") for match in _RECORD.finditer(text)))


def moved_records(text: str, listings: Mapping[str, Collection[str]]) -> dict[str, str]:
    """The records ``text`` names that aren't written yet but whose number another record in
    their directory has, each mapped to its path under the next free number there, one above
    the highest; ``listings`` holds the file names in each directory, by directory."""
    moves: dict[str, str] = {}
    highest = {name: _highest(files) for name, files in listings.items()}
    for match in _RECORD.finditer(text):
        path, directory, number = match[0], match[1].rstrip("/"), match[2]
        files = listings.get(directory, ())
        if path in moves or _name(path) in files or not _taken(number, files):
            continue
        highest[directory] += 1
        moves[path] = f"{match[1]}{highest[directory]:0{_WIDTH}d}-{match[3]}.md"
    return moves


def renumbered(text: str, moves: Mapping[str, str]) -> str:
    """``text`` with each record ``moves`` maps named by its new path instead."""
    return _RECORD.sub(lambda match: moves.get(match[0], match[0]), text)


def moved_note(old: str, new: str) -> str:
    """What the user is told when the record at ``old`` moved to ``new``."""
    return f"Renamed {old} to {new}: another record took its number."


def _name(path: str) -> str:
    return path.rpartition("/")[2]


def _taken(number: str, files: Collection[str]) -> bool:
    return any(name.startswith(f"{number}-") for name in files)


def _highest(files: Collection[str]) -> int:
    numbers = [int(match[1]) for name in files if (match := _NUMBERED.match(name))]
    return max(numbers, default=0)
