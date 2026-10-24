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
    their directory has, each mapped to its path there under another number: the one it's
    already written under, as when a task names a record by the number it had before it moved,
    or else the next free one, one above the highest; ``listings`` holds the file names in each
    directory, by directory."""
    moves: dict[str, str] = {}
    highest = {name: _highest(files) for name, files in listings.items()}
    for match in _RECORD.finditer(text):
        path, directory, number = match[0], match[1].rstrip("/"), match[2]
        files = listings.get(directory, ())
        if path in moves or _name(path) in files or not _taken(number, files):
            continue
        moves[path] = f"{match[1]}{_new_name(match[3], files, highest, directory)}"
    return moves


def _new_name(name: str, files: Collection[str], highest: dict[str, int], directory: str) -> str:
    """The file name of the record called ``name``, among ``files``, in ``directory``: the one
    it's written under, if any, or else under one above the ``highest`` number given there yet,
    which it then is."""
    written = _written(name, files)
    if written is not None:
        return written
    highest[directory] += 1
    return f"{highest[directory]:0{_WIDTH}d}-{name}.md"


def renumbered(text: str, moves: Mapping[str, str]) -> str:
    """``text`` with each record ``moves`` maps named by its new path instead."""
    return _RECORD.sub(lambda match: moves.get(match[0], match[0]), text)


def moved_note(old: str, new: str) -> str:
    """What the user is told when the record at ``old`` moved to ``new``."""
    return f"Renamed {old} to {new}: another record took its number."


def planner_note(old: str, new: str) -> str:
    """What the planner is told when the record at ``old`` moved to ``new``, so the tasks it
    proposes from then on name it where it is."""
    return (
        f"Another record took the number of `{old}`, so orbit renamed it `{new}` in the tasks "
        f"still to come. Name it `{new}` from now on."
    )


def _name(path: str) -> str:
    return path.rpartition("/")[2]


def _taken(number: str, files: Collection[str]) -> bool:
    return any(name.startswith(f"{number}-") for name in files)


def _written(name: str, files: Collection[str]) -> str | None:
    """The file in ``files`` holding the record called ``name`` under some number, if any."""
    return next(
        (file for file in files if _NUMBERED.match(file) and file[_WIDTH + 1 :] == f"{name}.md"),
        None,
    )


def _highest(files: Collection[str]) -> int:
    numbers = [int(match[1]) for name in files if (match := _NUMBERED.match(name))]
    return max(numbers, default=0)
