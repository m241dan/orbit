"""The code files a super review reads besides those the commits since the last one changed: a
sample of the project's source that rotates, so every code file comes up for review before any
comes up again.

Which super review last sampled each file is kept by its number, counting the repository's super
reviews from 1, so the rotation needs no clock."""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from orbit.agent.domain.mapped_paths import is_mapped_code

type Choose = Callable[[Sequence[str], int], Sequence[str]]
"""Chooses so many of the paths given, each once, at random, as :func:`random.sample` does."""


@dataclass(frozen=True)
class Draw:
    """How a sample is drawn: ``size`` files at most, those of the same standing in the rotation
    chosen by ``choose``."""

    size: int
    choose: Choose


def sample_files(
    tracked: Collection[str],
    mapped_paths: Collection[str],
    changed: Collection[str],
    sampled: Mapping[str, int],
    draw: Draw,
) -> tuple[str, ...]:
    """Up to ``draw.size`` of the eligible ``tracked`` files, sorted: those that are code under
    one of ``mapped_paths`` when any are given (a mapped file itself, or a file inside a mapped
    folder), as :func:`~orbit.agent.domain.mapped_paths.is_mapped_code` says, and not among the
    files ``changed``. Files never sampled come first, then those ``sampled`` longest ago (by
    the number of the super review that last sampled each), ``draw.choose`` picking among the
    files of the standing that doesn't fit whole. All of them when fewer are eligible; none for
    a size of 0."""
    unchanged = set(tracked) - set(changed)
    eligible = sorted(path for path in unchanged if is_mapped_code(path, mapped_paths))
    return tuple(sorted(_drawn(eligible, sampled, draw)))


def with_sample(
    sampled: Mapping[str, int], sample: Collection[str], tracked: Collection[str], number: int
) -> dict[str, int]:
    """``sampled``, the number of the super review that last sampled each file, by path, once
    super review ``number`` sampled ``sample``: those files at ``number``, and the files no
    longer among the ``tracked`` ones forgotten."""
    still = set(tracked)
    kept = {path: last for path, last in sampled.items() if path in still}
    return kept | dict.fromkeys(sample, number)


def _drawn(eligible: Sequence[str], sampled: Mapping[str, int], draw: Draw) -> list[str]:
    """Up to ``draw.size`` of the ``eligible`` files, a standing at a time, as
    :func:`sample_files` draws them."""
    drawn: list[str] = []
    for standing in _standings(eligible, sampled):
        wanted = min(draw.size - len(drawn), len(standing))
        if wanted <= 0:
            break
        drawn += draw.choose(standing, wanted)
    return drawn


def _standings(eligible: Sequence[str], sampled: Mapping[str, int]) -> list[list[str]]:
    """The ``eligible`` files grouped by when they were last ``sampled``, in the order the
    rotation takes them: those never sampled, then those sampled by the lowest number on."""
    groups: dict[tuple[bool, int], list[str]] = {}
    for path in eligible:
        groups.setdefault((path in sampled, sampled.get(path, 0)), []).append(path)
    return [groups[standing] for standing in sorted(groups)]
