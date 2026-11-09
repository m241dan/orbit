"""Reading the machine's size from the operating system: the CPUs the process may use and the
physical memory, each left unknown where it can't be read."""

import os
from collections.abc import Callable, Collection

from orbit.agent.domain.machine import Bytes, Machine

type Affinity = Callable[[int], Collection[int]]
"""``os.sched_getaffinity``: the CPUs a process, 0 for this one, may run on."""

type CpuCount = Callable[[], int | None]
"""``os.cpu_count``: the CPUs the system has, none when unknown."""

type Sysconf = Callable[[str], int]
"""``os.sysconf``: a system configuration value by name."""

SCHED_GETAFFINITY: Affinity | None = getattr(os, "sched_getaffinity", None)
"""This system's ``os.sched_getaffinity``; none where it has none, as on macOS."""

SYSCONF: Sysconf | None = getattr(os, "sysconf", None)
"""This system's ``os.sysconf``; none where it has none, as on Windows."""


def read_machine(
    affinity: Affinity | None = SCHED_GETAFFINITY,
    cpu_count: CpuCount = os.cpu_count,
    sysconf: Sysconf | None = SYSCONF,
) -> Machine:
    """The machine's size: the CPUs this process may use, from ``affinity``, else
    ``cpu_count`` where it's missing or fails, and the physical memory, from ``sysconf``'s pages
    and page size; each none when it can't be read."""
    from_affinity = None if affinity is None else _positive(lambda: len(affinity(0)))
    cpus = from_affinity if from_affinity is not None else _positive(cpu_count)
    return Machine(cpus, None if sysconf is None else _read_memory(sysconf))


def _read_memory(sysconf: Sysconf) -> Bytes | None:
    """The physical memory ``sysconf`` gives; none when it can't be read."""
    pages = _positive(lambda: sysconf("SC_PHYS_PAGES"))
    page_size = _positive(lambda: sysconf("SC_PAGE_SIZE"))
    return None if pages is None or page_size is None else Bytes(pages * page_size)


def _positive(read: Callable[[], int | None]) -> int | None:
    """The number ``read`` gives; none when it fails, gives none or gives one below 1."""
    try:
        value = read()
    except ValueError, OSError:
        return None
    return value if value is not None and value > 0 else None
