"""The code files a super review samples besides those changed since the last one."""

import random
from collections.abc import Sequence

from orbit.agent.domain.review_sample import CODE_EXTENSIONS, sample_files

TRACKED = ("src/a.py", "src/b.cpp", "docs/x.md", "uv.lock", "tests/t.py")


def first(paths: Sequence[str], size: int) -> list[str]:
    """The first ``size`` of ``paths``: a choice tests can predict."""
    return list(paths[:size])


def last(paths: Sequence[str], size: int) -> list[str]:
    """The last ``size`` of ``paths``, in reverse: a choice tests can predict."""
    return list(paths[::-1][:size])


def test_the_sample_is_the_code_files_under_the_mapped_paths() -> None:
    assert sample_files(TRACKED, ["src"], (), 10, first) == ("src/a.py", "src/b.cpp")


def test_without_mapped_paths_the_sample_is_every_code_file() -> None:
    assert sample_files(TRACKED, [], (), 10, first) == ("src/a.py", "src/b.cpp", "tests/t.py")


def test_the_sample_leaves_out_the_files_changed_since_the_last_super_review() -> None:
    assert sample_files(TRACKED, ["src"], ["src/a.py"], 10, first) == ("src/b.cpp",)


def test_a_mapped_file_is_sampled_but_not_a_folder_whose_name_it_starts() -> None:
    tracked = ("noxfile.py", "src/a.py", "srcs/b.py", "tools/c.py")
    assert sample_files(tracked, ["noxfile.py", "src"], (), 10, first) == ("noxfile.py", "src/a.py")


def test_mapped_paths_are_compared_once_normalised() -> None:
    tracked = ("src/a.py", "tests/t.py", "tools/c.py")
    assert sample_files(tracked, ["./src/", "tests/../tests"], (), 10, first) == (
        "src/a.py",
        "tests/t.py",
    )


def test_the_project_root_mapped_samples_every_code_file() -> None:
    assert sample_files(TRACKED, ["."], (), 10, first) == ("src/a.py", "src/b.cpp", "tests/t.py")


def test_more_eligible_than_the_size_samples_those_chosen_sorted() -> None:
    tracked = tuple(f"src/m{n}.py" for n in range(5))
    assert sample_files(tracked[::-1], [], (), 2, last) == ("src/m3.py", "src/m4.py")


def test_thirty_eligible_files_sample_ten_distinct_eligible_ones() -> None:
    tracked = (*(f"src/m{n:02}.rs" for n in range(30)), "README.md", "tests/t.py")
    sampled = sample_files(tracked, ["src"], (), 10, random.sample)
    assert len(set(sampled)) == len(sampled) == 10
    assert all(path.startswith("src/m") for path in sampled)
    assert list(sampled) == sorted(sampled)


def test_a_seeded_choice_samples_the_same_files_each_time() -> None:
    tracked = tuple(f"src/m{n:02}.go" for n in range(30))
    samples = {sample_files(tracked, ["src"], (), 10, random.Random(7).sample) for _ in range(3)}
    assert len(samples) == 1


def test_a_size_of_zero_samples_nothing() -> None:
    assert sample_files(TRACKED, [], (), 0, random.sample) == ()


def test_nothing_eligible_samples_nothing() -> None:
    assert sample_files(("docs/x.md", "uv.lock"), [], (), 10, random.sample) == ()


def test_the_code_extensions_are_the_built_in_source_ones() -> None:
    assert set(CODE_EXTENSIONS) == {
        ".py",
        ".pyi",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".cs",
        ".swift",
        ".rb",
    }


def test_each_code_extension_is_sampled() -> None:
    tracked = tuple(sorted(f"lib/file{extension}" for extension in CODE_EXTENSIONS))
    assert sample_files(tracked, [], (), len(tracked), first) == tracked
