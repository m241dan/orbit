"""The numbered requirements records tasks name, such as ``docs/00024-feature-metrics.md``, and
moving one whose number another record took by the time its task runs."""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence

# A relative path whose file name is a five-digit number, a dash and a name, ending in .md.
_RECORD = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*)(\d{5})-([\w.-]*?)\.md(?![\w-])")
_NUMBERED = re.compile(r"(\d{5})-")
_WIDTH = 5


def record_directories(text: str) -> tuple[str, ...]:
    """The directories of the records ``text`` names, each once in the order first named, ``""``
    for the top level."""
    return tuple(dict.fromkeys(match[1].rstrip("/") for match in _RECORD.finditer(text)))


def named_records(texts: Iterable[str]) -> tuple[str, ...]:
    """The records ``texts`` name, in the order named."""
    return tuple(match[0] for text in texts for match in _RECORD.finditer(text))


def moved_records(
    text: str, listings: Mapping[str, Collection[str]], named: Sequence[str] = ()
) -> dict[str, str]:
    """The records ``text`` names that aren't written yet but whose number another record in
    their directory has, each mapped to its path there under another number, as when a task
    names a record by the number it had before it moved: the one it's written under; or else
    the last of ``named``, the records other tasks name, that names it under a number no other
    record has; or else the next free one, one above the highest. ``listings`` holds the file
    names in each directory, by directory."""
    moves: dict[str, str] = {}
    highest = {name: _highest(files) for name, files in listings.items()}
    for match in _RECORD.finditer(text):
        files = listings.get(_directory(match), ())
        if match[0] not in moves and _clashes(match, files):
            moves[match[0]] = _new_path(match, files, named, highest)
    return moves


def _new_path(
    record: re.Match[str], files: Collection[str], named: Sequence[str], highest: dict[str, int]
) -> str:
    """Where ``record`` goes, as :func:`moved_records` says."""
    known = _written(record, files) or _planned(record, named, files)
    return known or _next_path(record, highest)


def _directory(record: re.Match[str]) -> str:
    return record[1].rstrip("/")


def _clashes(record: re.Match[str], files: Collection[str]) -> bool:
    """Whether ``record`` isn't among ``files`` but another record there has its number."""
    return _name(record[0]) not in files and _taken(record[2], files)


def _planned(record: re.Match[str], named: Sequence[str], files: Collection[str]) -> str | None:
    """The last of ``named`` that is ``record`` under a number none of ``files`` has, if any."""
    for path in reversed(named):
        other = _RECORD.fullmatch(path)
        if other is not None and _elsewhere(record, other) and not _taken(other[2], files):
            return path
    return None


def _elsewhere(record: re.Match[str], other: re.Match[str]) -> bool:
    """Whether ``other`` names ``record``, in the same directory, under another number."""
    return (other[1], other[3]) == (record[1], record[3]) and other[2] != record[2]


def _next_path(record: re.Match[str], highest: dict[str, int]) -> str:
    """``record``'s path under one above the ``highest`` number given in its directory yet,
    which it then is."""
    directory = _directory(record)
    highest[directory] += 1
    return f"{record[1]}{highest[directory]:0{_WIDTH}d}-{record[3]}.md"


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


def _written(record: re.Match[str], files: Collection[str]) -> str | None:
    """The path of the file in ``files`` holding ``record`` under some number, if any."""
    wanted = f"{record[3]}.md"
    name = next(
        (file for file in files if _NUMBERED.match(file) and file[_WIDTH + 1 :] == wanted), None
    )
    return None if name is None else f"{record[1]}{name}"


def _highest(files: Collection[str]) -> int:
    numbers = [int(match[1]) for name in files if (match := _NUMBERED.match(name))]
    return max(numbers, default=0)
