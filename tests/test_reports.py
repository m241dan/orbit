"""The rules for reports: what may be saved, and how the tools list and read them."""

from datetime import UTC, datetime
from typing import Any

import pytest

from orbit.agent.domain.reports import (
    NO_REPORTS,
    NO_TITLE,
    REPORT_LIMIT,
    TOO_LARGE,
    report_id,
    report_list,
    report_refusal,
    report_text,
    saved_note,
    unknown_report,
)
from orbit.protocol import ReportShown, ReportSummary

WHEN = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def test_a_report_with_a_title_and_a_body_within_the_limit_may_be_saved() -> None:
    assert report_refusal("Timings", "x" * REPORT_LIMIT) is None


def test_a_report_over_200_kb_is_refused() -> None:
    assert report_refusal("Timings", "x" * (300 * 1024)) == TOO_LARGE
    assert TOO_LARGE == (
        "That report is over 200 KB; summarise it and keep only the essential output."
    )


def test_the_limit_counts_bytes_rather_than_characters() -> None:
    assert report_refusal("Timings", "é" * (REPORT_LIMIT // 2 + 1)) == TOO_LARGE


@pytest.mark.parametrize("title", ["", "   "])
def test_a_report_without_a_title_is_refused(title: str) -> None:
    assert report_refusal(title, "body") == NO_TITLE


def test_the_saved_note_names_the_id() -> None:
    assert saved_note(3) == "Saved report 3."


def test_an_unknown_report_is_named() -> None:
    assert unknown_report(7) == "There's no report 7."


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"id": 2}, 2),
        ({"id": " 12 "}, 12),
        ({"id": "two"}, None),
        ({"id": True}, None),
        ({}, None),
        ({"id": 2**63 - 1}, 2**63 - 1),
        ({"id": 2**63}, None),
        ({"id": "99999999999999999999"}, None),
        ({"id": "9" * 5000}, None),
        ({"id": "²"}, None),
        ({"id": "①"}, None),
    ],
)
def test_a_report_id_is_a_whole_number_or_its_digits(
    arguments: dict[str, Any], expected: int | None
) -> None:
    assert report_id(arguments) == expected


def test_reports_are_listed_a_line_each_with_id_title_author_and_date() -> None:
    reports = (
        ReportSummary(id=2, title="Memory", author="planner", time=WHEN),
        ReportSummary(id=1, title="Timings", author="experimenter", time=WHEN),
    )
    assert report_list(reports) == (
        "2. Memory (by the planner, 2026-10-25)\n1. Timings (by the experimenter, 2026-10-25)"
    )


def test_no_reports_are_listed_as_a_note() -> None:
    assert report_list(()) == NO_REPORTS


def test_a_report_reads_as_its_title_then_its_body() -> None:
    report = ReportShown(id=1, title="Timings", body="It took 3s.", author="planner", time=WHEN)
    assert report_text(report) == "# Timings\n\nIt took 3s."
