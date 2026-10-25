"""Adding up what turns cost: today's and lifetime totals, with the spend recorded only in
sessions' records."""

from orbit.agent.domain.usage_totals import SpendTotals, counted, earlier_spend, usage_totals
from orbit.protocol import Usage


def test_a_rows_usage_keeps_only_tokens_and_cost() -> None:
    usage = Usage(
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        cost_usd=0.5,
        credits=0.25,
        turns=6,
        duration_ms=7,
    )
    assert counted(usage) == Usage(
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        cost_usd=0.5,
        credits=0.25,
    )


def test_earlier_spend_is_what_the_record_spent_beyond_the_rows() -> None:
    spent = Usage(input_tokens=300, output_tokens=50, cost_usd=2.0, credits=1.5, turns=5)
    recorded = Usage(input_tokens=100, output_tokens=80, cost_usd=0.5, credits=0.5)
    assert earlier_spend(spent, recorded) == Usage(input_tokens=200, cost_usd=1.5, credits=1.0)


def test_totals_count_today_the_rows_given_and_lifetime_every_row_and_earlier_spend() -> None:
    totals = usage_totals(
        today={"w1": Usage(input_tokens=10)},
        recorded={
            ("w1", "s1"): Usage(input_tokens=30),
            ("w1", "gone"): Usage(input_tokens=5),
            ("w2", "s2"): Usage(output_tokens=7),
        },
        spent={("w1", "s1"): Usage(input_tokens=100), ("w3", "old"): Usage(cost_usd=1.0)},
    )
    assert totals.workspaces == {
        "w1": SpendTotals(Usage(input_tokens=10), Usage(input_tokens=105)),
        "w2": SpendTotals(Usage(), Usage(output_tokens=7)),
        "w3": SpendTotals(Usage(), Usage(cost_usd=1.0)),
    }
    assert totals.total == SpendTotals(
        Usage(input_tokens=10), Usage(input_tokens=105, output_tokens=7, cost_usd=1.0)
    )


def test_with_nothing_recorded_nor_spent_the_totals_are_zero() -> None:
    totals = usage_totals({}, {}, {})
    assert (totals.workspaces, totals.total) == ({}, SpendTotals())
