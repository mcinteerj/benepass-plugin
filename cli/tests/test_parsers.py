"""The two parsers every command shares, against the shapes Benepass really emits.

Both read fields that are rendered rather than specified — a date written in
whichever format the client that filed it used, and a money string written in
whichever locale the currency belongs to. Neither is documented anywhere, so
each shape here was taken off a live payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from benepass import output


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09/13/2026", "2026-09-13"),  # what this CLI submits
        ("2026-09-13", "2026-09-13"),  # plain ISO
        ("2025-12-29T13:00:00.000Z", "2025-12-29"),  # what the web app writes
        ("2026-09-13T00:00:00Z", "2026-09-13"),
        ("", None),
        (None, None),
        ("not a date", None),
        ("13/09/2026", None),  # DD/MM is not a shape Benepass produces
    ],
)
def test_every_date_shape_in_live_data_reads(raw: Any, expected: str | None) -> None:
    assert output.parse_date(raw) == expected


def test_a_claims_purchase_date_is_not_its_filing_date() -> None:
    """The gap between them is months on a backfilled receipt."""
    txn = {
        "id": "expense_0000000000000000000001",
        "transaction_time": "2026-08-30T22:04:47.945301Z",
        "claim": {
            "substantiation_items": [
                {"item_type": "note", "item_detail": {"value": "annual renewal"}},
                {
                    "item_type": "purchase_date",
                    "item_detail": {"value": "2025-12-29T13:00:00.000Z"},
                },
            ]
        },
    }

    assert output.claim_purchase_date(txn) == "2025-12-29"


def test_a_claim_without_a_purchase_date_says_so() -> None:
    """Live claims exist with no purchase_date item; None is the honest answer."""
    assert output.claim_purchase_date({"claim": {"substantiation_items": []}}) is None
    assert output.claim_purchase_date({"claim": None}) is None
    assert output.claim_purchase_date({}) is None


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("$861.35", 861.35),
        ("-$29.00", 29.00),
        ("-Rp1.357.560,00", 1357560.00),  # dot thousands, comma decimal
        ("1 234,56 kr", 1234.56),  # space thousands, comma decimal
        ("¥1,234", 1234.0),  # comma thousands, no decimals
        ("£48.20", 48.20),
        ("$0.00", 0.0),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_money_parses_out_of_every_locale_benepass_formats_in(
    rendered: Any, expected: float | None
) -> None:
    got = output.parse_money(rendered)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_a_lone_separator_with_three_digits_offers_both_readings() -> None:
    """`KD153.750` is 153,750 in most currencies and 153.75 in the five with
    three minor digits, and the string cannot say which.

    `parse_money` has to pick one, so it keeps the common reading; a caller
    with a second source of truth — a published rate, a currency's own
    `decimals` — takes both and decides. `profile` is that caller.
    """
    assert output.money_readings("KD153.750") == [153750.0, 153.75]
    assert output.parse_money("KD153.750") == 153750.0


def test_an_unambiguous_rendering_offers_exactly_one_reading() -> None:
    """Both separators present, or anything but three digits after the last one."""
    assert output.money_readings("KD1,537.500") == [1537.5]
    assert output.money_readings("$861.35") == [861.35]
    assert output.money_readings("£48.20") == [48.20]
    assert output.money_readings("n/a") == []


def test_the_shape_is_what_is_ambiguous_not_the_size_of_the_number() -> None:
    """`¥1,234` is 1,234 yen, and would be 1.234 in a three-minor-digit currency.

    Both readings come back here too. The caller that uses the second one
    (`profile`) offers it only to currencies publishing three minor digits,
    which is what stops a yen account being answered with a rate 1000x out.
    """
    assert output.money_readings("¥1,234") == [1234.0, 1.234]
    assert output.parse_money("¥1,234") == 1234.0
