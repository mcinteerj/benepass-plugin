"""`benepass match` — the search whose "no" has to be trustworthy.

This command is the double-dip check: an agent runs it before filing a receipt
and files when it comes back empty. So every failure here is silent and costs
money — a card charge missed because the search was bounded above, a claim
missed because its purchase date was read as its filing date, a false match on
the employee-local conversion rather than the receipt's own figure.

Rows are shaped field-for-field like live `transactions --json` payloads, with
invented merchants and ids.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from benepass import cli_match
from benepass.cli import app

runner = CliRunner()

# A card swipe: the benefit card already paid, so a reimbursement for the same
# purchase would claim it twice. The descriptor is nothing like the merchant's
# own brand, which is why this command searches on amount rather than name.
CARD_ROW: dict[str, Any] = {
    "id": "ictxn_0000000000000000000001",
    "transaction_type": "card",
    "transaction_status": "complete",
    "transaction_time": "2026-08-25T09:13:00Z",
    "merchant_name": "TRANSIT AUTH TAP",
    "merchant_currency": {"code": "XND"},
    "merchant_amount": -3140,
    "formatted_merchant_amount": "-$31.40",
    "amount": -2512,
    "formatted_local_amount": "-$31.40",
    "account": {
        "id": "account_0000000000000000000001",
        "enrollment": {"benefit": {"id": "benefit_x", "name": "Commuting"}},
    },
}

# Filed ten months after the purchase it is for: the row this command must
# still find, and the reason the fetch is not bounded above.
LATE_FILED_CLAIM: dict[str, Any] = {
    "id": "expense_0000000000000000000002",
    "transaction_type": "reimbursement",
    "transaction_status": "pending",
    "transaction_time": "2026-08-30T22:04:47Z",
    "merchant_name": "Example News",
    "merchant_currency": {"code": "USD"},
    "merchant_amount": -6400,
    "formatted_merchant_amount": "-$64.00",
    "amount": -6400,
    "formatted_local_amount": "-$80.00",
    "account": {
        "id": "account_0000000000000000000002",
        "enrollment": {"benefit": {"id": "benefit_y", "name": "Education"}},
    },
}

# A foreign card row. Its LOCAL conversion is $98.75 and its receipt says
# ¤987.654,00 — searching 98.75 must not match it, or a genuine $98.75 receipt
# gets refused as a duplicate of a hotel bill on the other side of the world.
FOREIGN_CARD_ROW: dict[str, Any] = {
    "id": "ictxn_0000000000000000000003",
    "transaction_type": "card",
    "transaction_status": "complete",
    "transaction_time": "2026-08-29T11:23:25Z",
    "merchant_name": "EXAMPLE HOTEL",
    "merchant_currency": {"code": "XMU"},
    "merchant_amount": -98765400,
    "formatted_merchant_amount": "-¤987.654,00",
    "amount": -7900,
    "formatted_local_amount": "-$98.75",
    "account": {
        "id": "account_0000000000000000000003",
        "enrollment": {"benefit": {"id": "benefit_z", "name": "Wellness"}},
    },
}

# The same printed figure as the card row under a different currency code —
# two currencies sharing the `$` symbol is the ordinary case. What --currency is for.
SHARED_FIGURE_ROW: dict[str, Any] = {
    "id": "expense_0000000000000000000004",
    "transaction_type": "reimbursement",
    "transaction_status": "complete",
    "transaction_time": "2026-08-26T00:00:00Z",
    "merchant_name": "Example Bookshop",
    "merchant_currency": {"code": "XSK"},
    "merchant_amount": -3140,
    "formatted_merchant_amount": "-$31.40",
    "amount": -1800,
    "formatted_local_amount": "-$22.50",
    "account": {
        "id": "account_0000000000000000000004",
        "enrollment": {"benefit": {"id": "benefit_z", "name": "Wellness"}},
    },
}

# Money arriving, not a purchase — it has no merchant amount at all and must
# never appear as something already claimed.
CONTRIBUTION_ROW: dict[str, Any] = {
    "id": "ictxn_0000000000000000000005",
    "transaction_type": "employer_contribution",
    "transaction_status": "complete",
    "transaction_time": "2026-08-25T00:00:00Z",
    "merchant_name": None,
    "merchant_currency": None,
    "merchant_amount": None,
    "formatted_merchant_amount": None,
    "amount": 3140,
    "formatted_local_amount": "$31.40",
    "account": {"id": "account_0000000000000000000001", "enrollment": {}},
}

CLAIM_DETAIL = {
    LATE_FILED_CLAIM["id"]: {
        **LATE_FILED_CLAIM,
        "claim": {
            "substantiation_items": [
                {
                    "item_type": "purchase_date",
                    "item_detail": {"value": "2025-11-04T13:00:00.000Z"},
                }
            ]
        },
    },
    SHARED_FIGURE_ROW["id"]: {
        **SHARED_FIGURE_ROW,
        "claim": {
            "substantiation_items": [
                {"item_type": "purchase_date", "item_detail": {"value": "08/22/2026"}}
            ]
        },
    },
}

ALL_ROWS = [
    CARD_ROW,
    LATE_FILED_CLAIM,
    FOREIGN_CARD_ROW,
    SHARED_FIGURE_ROW,
    CONTRIBUTION_ROW,
]


class FakeClient:
    """Records what was asked for, so the fetch's shape can be asserted on."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[dict[str, Any]] = []
        self.detail_ids: list[str] = []
        self.accounts_called = 0

    def transactions(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or 100)
        return {
            "data": self.rows[offset : offset + limit],
            "total_count": len(self.rows),
        }

    def transaction(self, txn_id: str) -> dict[str, Any]:
        self.detail_ids.append(txn_id)
        return CLAIM_DETAIL.get(txn_id, {"id": txn_id})

    def accounts(self) -> list[dict[str, Any]]:
        self.accounts_called += 1
        return []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient(list(ALL_ROWS))
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))
    return fake


def _matches(output_text: str) -> list[dict[str, Any]]:
    """The rows out of the --json envelope, which also carries `truncated`."""
    payload = json.loads(output_text)
    assert set(payload) == {"truncated", "matches"}
    return list(payload["matches"])


def _ids(output_text: str) -> list[str]:
    return [row["id"] for row in _matches(output_text)]


def test_a_card_charge_at_the_same_amount_and_date_is_found(client: FakeClient) -> None:
    """The double-dip itself: the benefit card already paid for this."""
    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    assert result.exit_code == 0
    assert CARD_ROW["id"] in _ids(result.stdout)


def test_a_claim_filed_months_late_is_matched_on_its_purchase_date(
    client: FakeClient,
) -> None:
    """Matching on transaction_time would date this row to August, not November."""
    result = runner.invoke(
        app,
        ["match", "--amount", "64.00", "--date", "2025-11-04", "--json"],
    )

    assert result.exit_code == 0
    found = _matches(result.stdout)
    assert [row["id"] for row in found] == [LATE_FILED_CLAIM["id"]]
    assert found[0]["date"] == "2025-11-04"


def test_the_claims_pass_is_never_bounded_above(client: FakeClient) -> None:
    """An `until` would hide the late-filed claim: filing date is not purchase date."""
    runner.invoke(app, ["match", "--amount", "64.00", "--date", "2025-11-04"])

    claims = [q for q in client.queries if q.get("kind") == "reimbursement"]
    assert claims, "reimbursements were never fetched"
    for query in claims:
        assert query["since"] == "2025-11-01"
        assert not query.get("until")


def test_the_card_pass_is_bounded_both_ways(client: FakeClient) -> None:
    """A card row's transaction time IS its purchase time, so the window bounds it.

    Leaving this pass open-ended is what made an old date page through years of
    history and risk the ceiling — the one condition under which an empty
    answer cannot be trusted.
    """
    runner.invoke(app, ["match", "--amount", "64.00", "--date", "2025-11-04"])

    cards = [q for q in client.queries if q.get("kind") == "card"]
    assert cards, "card rows were never fetched"
    for query in cards:
        assert query["since"] == "2025-11-01"
        assert query["until"] == "2025-11-08"


def test_a_dateless_scan_is_one_unbounded_pass(client: FakeClient) -> None:
    runner.invoke(app, ["match", "--amount", "64.00", "--days", "30"])

    assert client.queries, "no transactions were fetched"
    for query in client.queries:
        assert not query.get("until")
        assert not query.get("kind")


def test_a_row_returned_by_both_passes_is_listed_once(client: FakeClient) -> None:
    """Benepass ignores a filter it does not recognise and still returns 200."""
    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    ids = _ids(result.stdout)
    # The card row comes back from both passes, because this fake ignores
    # `kind` exactly as Benepass ignores a parameter it does not recognise.
    # Merging by id is what stops it being reported twice.
    assert ids.count(CARD_ROW["id"]) == 1
    # The bookshop claim is a second genuine match, not a duplicate: the same
    # printed figure in another currency, three days inside the window, and no
    # --currency passed to tell them apart. Both rows are the right answer.
    assert set(ids) == {CARD_ROW["id"], SHARED_FIGURE_ROW["id"]}


def test_the_local_conversion_is_never_what_gets_matched(client: FakeClient) -> None:
    """$98.75 is this row's converted figure; its receipt says ¤987.654,00."""
    result = runner.invoke(
        app, ["match", "--amount", "98.75", "--date", "2026-08-29", "--json"]
    )

    assert result.exit_code == 0
    assert _ids(result.stdout) == []


def test_the_receipts_own_figure_matches_in_minor_units(client: FakeClient) -> None:
    result = runner.invoke(
        app,
        ["match", "--amount", "987654", "--currency", "XMU", "--json"],
    )

    assert _ids(result.stdout) == [FOREIGN_CARD_ROW["id"]]


def test_currency_flags_the_other_row_rather_than_dropping_it(
    client: FakeClient,
) -> None:
    """--currency is advisory: it marks a row, it never removes one.

    Two currencies sharing the `$` symbol is the case the flag exists for. But
    Benepass's own label on a row can simply be wrong — a card row has arrived
    carrying a currency the purchase was not made in — so a filter on the code
    answers "No matches" for a charge that is sitting in the list, which is the
    one answer this command must never give.
    """
    both = runner.invoke(app, ["match", "--amount", "31.40", "--json"])
    assert set(_ids(both.stdout)) == {CARD_ROW["id"], SHARED_FIGURE_ROW["id"]}

    flagged = runner.invoke(
        app, ["match", "--amount", "31.40", "--currency", "xsk", "--json"]
    )
    payload = json.loads(flagged.stdout)
    assert set(_ids(flagged.stdout)) == {CARD_ROW["id"], SHARED_FIGURE_ROW["id"]}
    by_id = {row["id"]: row for row in payload["matches"]}
    assert by_id[SHARED_FIGURE_ROW["id"]]["currency_mismatch"] is False
    assert by_id[CARD_ROW["id"]]["currency_mismatch"] is True


def test_a_mislabelled_row_is_never_hidden_by_currency(client: FakeClient) -> None:
    """The live failure: the charge existed, and --currency said "No matches".

    Benepass labelled the card row with a currency the purchase was not made
    in, so the double-dip guard answered no for a charge already on file.
    Amount and date select the rows; the currency only annotates them.
    """
    result = runner.invoke(
        app,
        ["match", "--amount", "31.40", "--date", "2026-08-25", "--currency", "XSK"],
    )

    assert result.exit_code == 0
    assert "No matches" not in result.stdout
    assert CARD_ROW["id"] in result.stdout
    assert "ccy\u2260" in result.stdout
    assert "ADVISORY" in result.stdout


def test_contributions_and_expirations_are_not_purchases(client: FakeClient) -> None:
    """A pot being topped up is not something anyone can claim twice."""
    result = runner.invoke(app, ["match", "--amount", "31.40", "--json"])

    assert CONTRIBUTION_ROW["id"] not in _ids(result.stdout)


def test_a_claim_with_no_purchase_date_is_shown_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missed duplicate costs money; a flagged row costs a second of reading."""
    dateless = {**SHARED_FIGURE_ROW, "id": "expense_0000000000000000000009"}
    fake = FakeClient([dateless])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-26", "--json"]
    )

    found = _matches(result.stdout)
    assert [row["id"] for row in found] == [dateless["id"]]
    assert found[0]["date_is_filing_date"] is True
    assert found[0]["date"] == "2026-08-26"


def test_a_dateless_claim_survives_a_date_window_it_cannot_be_judged_against(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filing date is not the purchase date, so it must not be filtered on.

    A claim filed months after the purchase, carrying no purchase date at all,
    is the shape this command exists to catch: judging it on its filing date
    drops it, and the sweep is told "No matches" for a receipt already on file.
    """
    dateless = {**LATE_FILED_CLAIM, "id": "expense_0000000000000000000010"}
    fake = FakeClient([dateless])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "64.00", "--date", "2025-11-04", "--json"]
    )

    found = _matches(result.stdout)
    assert [row["id"] for row in found] == [dateless["id"]]
    assert found[0]["date_is_filing_date"] is True
    # Shown as what it is — the filing date, ten months after the purchase.
    assert found[0]["date"] == "2026-08-30"


def test_a_card_row_with_an_unreadable_timestamp_is_kept_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same reason: an unreadable date is unknown, not "a different purchase"."""
    undated = {**CARD_ROW, "id": "ictxn_0000000000000000000011", "transaction_time": ""}
    fake = FakeClient([undated])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    assert [row["id"] for row in _matches(result.stdout)] == [undated["id"]]


def test_a_claim_detail_is_fetched_only_for_rows_whose_amount_already_matched(
    client: FakeClient,
) -> None:
    """One extra API call per reimbursement row is the cost this ordering avoids."""
    runner.invoke(app, ["match", "--amount", "64.00", "--date", "2025-11-04"])

    assert client.detail_ids == [LATE_FILED_CLAIM["id"]]


def test_the_amount_tolerance_is_half_a_cent(client: FakeClient) -> None:
    assert _ids(
        runner.invoke(app, ["match", "--amount", "31.404", "--json"]).stdout
    ) == [CARD_ROW["id"], SHARED_FIGURE_ROW["id"]]
    assert (
        _ids(runner.invoke(app, ["match", "--amount", "31.46", "--json"]).stdout) == []
    )


def test_no_matches_is_an_answer_not_a_failure(client: FakeClient) -> None:
    result = runner.invoke(app, ["match", "--amount", "4242.42"])

    assert result.exit_code == 0
    assert "No matches" in result.stdout


def test_the_scanned_range_is_stated_so_an_empty_answer_can_be_judged(
    client: FakeClient,
) -> None:
    result = runner.invoke(app, ["match", "--amount", "4242.42", "--days", "30"])

    assert "the last 30 days" in result.stdout


def test_an_ambiguous_date_is_refused_before_anything_is_fetched(
    client: FakeClient,
) -> None:
    """03/09 is two different days, and a wrong "no matches" is how a double-dip happens."""
    result = runner.invoke(app, ["match", "--amount", "10", "--date", "03/09/2026"])

    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.stderr
    assert client.queries == []


def test_a_non_positive_amount_is_refused(client: FakeClient) -> None:
    result = runner.invoke(app, ["match", "--amount", "0"])

    assert result.exit_code == 1
    assert client.queries == []


def test_paging_walks_past_the_first_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """total_count is honoured, so a match on page two is not invisible."""
    filler = [
        {**CONTRIBUTION_ROW, "id": f"ictxn_filler{i:019d}"}
        for i in range(cli_match.PAGE_SIZE)
    ]
    fake = FakeClient([*filler, CARD_ROW])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(app, ["match", "--amount", "31.40", "--json"])

    assert _ids(result.stdout) == [CARD_ROW["id"]]
    assert [q["offset"] for q in fake.queries] == [0, cli_match.PAGE_SIZE]


class ShortPageClient(FakeClient):
    """A server that hands back fewer rows than asked for, and says so in total_count.

    Benepass is unofficial and caps whatever it feels like capping. Treating a
    short page as the end of the data is how this command answers a confident
    "no matches" over a row it never fetched.
    """

    PAGE_CAP = 50

    def transactions(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        offset = int(kwargs.get("offset") or 0)
        return {
            "data": self.rows[offset : offset + self.PAGE_CAP],
            "total_count": len(self.rows),
        }


def test_a_page_shorter_than_asked_for_is_not_the_end_of_the_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """total_count decides completeness, never a page merely arriving short."""
    filler = [{**CONTRIBUTION_ROW, "id": f"ictxn_filler{i:019d}"} for i in range(60)]
    fake = ShortPageClient([*filler, CARD_ROW])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    payload = json.loads(result.stdout)
    assert payload["truncated"] is False
    assert [row["id"] for row in payload["matches"]] == [CARD_ROW["id"]]
    # The second request starts where the first one actually finished, not at a
    # fixed stride — a 50-row page with a 100-row stride would skip rows 50-99.
    assert [q["offset"] for q in fake.queries if q.get("kind") == "card"] == [0, 50]


class OffsetIgnoringClient(FakeClient):
    """Ignores `offset` — the documented Benepass habit with parameters it dislikes."""

    def transactions(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return {"data": list(self.rows)}


def test_a_server_ignoring_offset_is_read_once_not_twenty_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page with nothing new on it stops the scan — as INCOMPLETE, not as a "no".

    The server handing back a page it has already handed over says nothing
    about what lies beyond it, so the scan ends (twenty copies of page one help
    nobody) and says it never finished.
    """
    fake = OffsetIgnoringClient([CARD_ROW])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    payload = json.loads(result.stdout)
    assert payload["truncated"] is True
    assert [row["id"] for row in payload["matches"]] == [CARD_ROW["id"]]
    assert len(fake.queries) < cli_match.MAX_PAGES


class OffsetIgnoringCountingClient(OffsetIgnoringClient):
    """Ignores `offset`, but reports the count — so one page IS all of it."""

    def transactions(self, **kwargs: Any) -> dict[str, Any]:
        payload = super().transactions(**kwargs)
        return {**payload, "total_count": len(self.rows)}


def test_a_server_that_reports_the_count_is_believed_when_it_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completeness has to stay reachable, or every dedup answer is "unknown"."""
    fake = OffsetIgnoringCountingClient([CARD_ROW])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    payload = json.loads(result.stdout)
    assert payload["truncated"] is False
    assert [row["id"] for row in payload["matches"]] == [CARD_ROW["id"]]


class OffsetCappingClient(FakeClient):
    """Caps `offset` — beyond the cap it re-serves the last page it will give."""

    CAP = cli_match.PAGE_SIZE

    def transactions(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        offset = min(int(kwargs.get("offset") or 0), self.CAP)
        limit = int(kwargs.get("limit") or 100)
        return {
            "data": self.rows[offset : offset + limit],
            "total_count": len(self.rows),
        }


def test_a_server_that_stops_short_of_its_own_count_is_not_a_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matching row sits past the cap, so "no matches" here would be a lie."""
    filler = [
        {**CONTRIBUTION_ROW, "id": f"ictxn_filler{i:019d}"}
        for i in range(cli_match.PAGE_SIZE * 3)
    ]
    fake = OffsetCappingClient([*filler, CARD_ROW])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    payload = json.loads(result.stdout)
    assert payload["matches"] == []
    assert payload["truncated"] is True
    assert "SCAN INCOMPLETE" in result.stderr


def test_a_row_with_no_stated_currency_is_flagged_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unstated is unknown, not "definitely not yours".

    Every skill call site passes --currency, so narrowing an unlabelled row
    away would hide exactly the duplicate nobody can check by hand — and the
    user would be told "No matches".
    """
    unlabelled = {
        **CARD_ROW,
        "id": "ictxn_0000000000000000000007",
        "merchant_currency": None,
    }
    fake = FakeClient([unlabelled])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app,
        ["match", "--amount", "31.40", "--date", "2026-08-25", "--currency", "XND"],
    )

    assert unlabelled["id"] in result.stdout
    assert "ccy\u2260" in result.stdout
    assert "ADVISORY" in result.stdout


def _ceiling_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """More rows than the runaway guard allows, so the scan stops short."""
    filler = [
        {**CONTRIBUTION_ROW, "id": f"ictxn_filler{i:019d}"}
        for i in range(cli_match.MAX_PAGES * cli_match.PAGE_SIZE + cli_match.PAGE_SIZE)
    ]
    fake = FakeClient(filler)
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))
    return fake


def test_a_truncated_scan_never_reads_as_a_plain_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole value of this command is that its "no" can be trusted."""
    _ceiling_client(monkeypatch)

    result = runner.invoke(app, ["match", "--amount", "4242.42"])

    assert result.exit_code == 0
    assert "SCAN INCOMPLETE" in result.stdout
    assert "not a 'no'" in result.stdout.replace("NOT", "not")


def test_a_truncated_scan_says_so_in_the_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ceiling_client(monkeypatch)

    result = runner.invoke(app, ["match", "--amount", "4242.42", "--json"])

    payload = json.loads(result.stdout)
    assert payload == {"truncated": True, "matches": []}


def test_a_complete_scan_says_it_is_complete(client: FakeClient) -> None:
    result = runner.invoke(app, ["match", "--amount", "4242.42", "--json"])

    assert json.loads(result.stdout) == {"truncated": False, "matches": []}
    assert "SCAN INCOMPLETE" not in result.stdout


# --------------------------------------------------------------------------
# Minor units: the scale is read off the row, not assumed
# --------------------------------------------------------------------------

# A currency recorded in major units — no minor unit at all, the JPY/KRW shape.
# Divided by 100 this row reads as 120, and a 12,000 receipt would be told
# "No matches" while the card had already paid for it.
ZERO_DECIMAL_ROW: dict[str, Any] = {
    **CARD_ROW,
    "id": "ictxn_0000000000000000000012",
    "merchant_currency": {"code": "XZD"},
    "merchant_amount": -12000,
    "formatted_merchant_amount": "-¤12,000",
}

# Three minor digits (the KWD/BHD/OMR/JOD/TND shape), in both conventions: the
# row's own rendering says 153.750 either way, and that is what decides.
THREE_DIGIT_ROWS: list[dict[str, Any]] = [
    {
        **CARD_ROW,
        "id": "ictxn_0000000000000000000013",
        "merchant_currency": {"code": "KWD"},
        "merchant_amount": minor,
        "formatted_merchant_amount": "-KD153.750",
    }
    for minor in (-153750, -15375)
]


@pytest.mark.parametrize(
    "row, amount",
    [
        (ZERO_DECIMAL_ROW, "12000"),
        (THREE_DIGIT_ROWS[0], "153.75"),
        (THREE_DIGIT_ROWS[1], "153.75"),
    ],
)
def test_the_row_s_own_rendering_decides_the_scale(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], amount: str
) -> None:
    """A fixed /100 is 100x out on a major-unit currency and 10x on a 3-digit one."""
    fake = FakeClient([row])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", amount, "--date", "2026-08-25", "--json"]
    )

    assert [found["id"] for found in _matches(result.stdout)] == [row["id"]]


def test_a_major_unit_row_does_not_match_a_hundredth_of_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old reading, 120, must not come back as a match for a 12,000 charge."""
    fake = FakeClient([ZERO_DECIMAL_ROW])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "120", "--date", "2026-08-25", "--json"]
    )

    assert _matches(result.stdout) == []


def test_a_row_with_no_rendering_falls_back_to_hundredths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to check against, so the only scale ever observed live stands."""
    bare = {
        **CARD_ROW,
        "id": "ictxn_0000000000000000000014",
        "formatted_merchant_amount": None,
    }
    fake = FakeClient([bare])
    monkeypatch.setattr(cli_match, "run", lambda fn, *a, **k: fn(fake))

    result = runner.invoke(
        app, ["match", "--amount", "31.40", "--date", "2026-08-25", "--json"]
    )

    assert [row["id"] for row in _matches(result.stdout)] == [bare["id"]]
