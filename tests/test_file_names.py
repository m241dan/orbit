from pathlib import Path

from orbit.agent.external.file_names import file_names


def test_the_files_in_a_directory_are_named_without_its_directories(tmp_path: Path) -> None:
    (tmp_path / "00001-feature-a.md").write_text("a")
    (tmp_path / "protocol.md").write_text("p")
    (tmp_path / "bugs").mkdir()
    assert sorted(file_names(str(tmp_path))) == ["00001-feature-a.md", "protocol.md"]


def test_a_directory_that_cant_be_read_has_no_files(tmp_path: Path) -> None:
    assert file_names(str(tmp_path / "missing")) == ()
    (tmp_path / "file").write_text("f")
    assert file_names(str(tmp_path / "file")) == ()
