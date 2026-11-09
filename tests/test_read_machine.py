"""Reading the machine's size from the operating system, each fact left out when it can't be
read."""

from collections.abc import Callable

from orbit.agent.domain.machine import Bytes, Machine
from orbit.agent.external.machine import read_machine

PAGES = {"SC_PHYS_PAGES": 2_000_000, "SC_PAGE_SIZE": 4096}


def affinity_of(count: int) -> Callable[[int], set[int]]:
    """A ``sched_getaffinity`` giving ``count`` CPUs for the process it's asked about, 0."""

    def affinity(pid: int) -> set[int]:
        assert pid == 0
        return set(range(count))

    return affinity


def sysconf_of(values: dict[str, int]) -> Callable[[str], int]:
    """A ``sysconf`` giving ``values`` by name."""
    return values.__getitem__


def failing(_: int) -> set[int]:
    """A ``sched_getaffinity`` that fails as the operating system can."""
    raise OSError("not here")


def uncounted() -> int | None:
    """A ``cpu_count`` that fails as the operating system can."""
    raise OSError("not here")


def refusing(_: str) -> int:
    """A ``sysconf`` that doesn't know the name it's asked for."""
    raise ValueError("unrecognized configuration name")


def test_it_reads_the_cpus_the_process_may_use_and_the_physical_memory() -> None:
    machine = read_machine(affinity_of(3), lambda: 16, sysconf_of(PAGES))
    assert machine == Machine(3, Bytes(2_000_000 * 4096))


def test_without_affinity_it_counts_the_cpus() -> None:
    assert read_machine(None, lambda: 8, sysconf_of(PAGES)).cpus == 8


def test_when_affinity_fails_it_counts_the_cpus() -> None:
    assert read_machine(failing, lambda: 8, sysconf_of(PAGES)).cpus == 8


def test_an_unknown_sysconf_name_leaves_the_memory_out() -> None:
    assert read_machine(affinity_of(2), lambda: 2, refusing) == Machine(cpus=2)


def test_a_non_positive_page_count_leaves_the_memory_out() -> None:
    pages = sysconf_of({"SC_PHYS_PAGES": -1, "SC_PAGE_SIZE": -1})
    assert read_machine(affinity_of(2), lambda: 2, pages).memory is None


def test_with_everything_failing_it_knows_nothing() -> None:
    assert read_machine(failing, lambda: None, refusing) == Machine()


def test_with_nothing_to_call_it_knows_nothing() -> None:
    assert read_machine(None, uncounted, None) == Machine()


def test_it_reads_this_machine() -> None:
    assert isinstance(read_machine(), Machine)
