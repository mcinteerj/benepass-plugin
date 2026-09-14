"""`benepass profile` — the facts a setup interview reads instead of asking for.

Two of them are derived rather than read, and both are wrong in a way that is
hard to notice: the local currency (Benepass names it nowhere, it only renders
into it) and the refresh cadence (there is no cadence field, only a schedule of
events). The third risk is the opposite — a field that exists, is populated, and
means something other than it appears to: `legal_address` is the EMPLOYER's
address on a multinational account, so reporting it as the user's city would
send an interview off asking about the wrong continent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from benepass import cli_profile, session
from benepass.cli import app

runner = CliRunner()

# Rates as the currency endpoint publishes them: units per USD. The codes are
# invented, because the point of every case below is the arithmetic, and a real
# code invites a reader to check it against today's real rate.
CURRENCIES = [
    {
        "id": "crcy_0000000000000000000001",
        "name": "US dollar",
        "code": "USD",
        "symbol": "$",
        "decimals": 2,
        "latest_rate_amount": 1.0,
    },
    {
        "id": "crcy_0000000000000000000002",
        "name": "Northern dollar",
        "code": "XND",
        "symbol": "$",
        "decimals": 2,
        "latest_rate_amount": 1.25,
    },
    {
        "id": "crcy_0000000000000000000003",
        "name": "Southern krona",
        "code": "XSK",
        "symbol": "kr",
        "decimals": 2,
        "latest_rate_amount": 9.4,
    },
]


def _account(account_id: str, usd_cents: float, local: str) -> dict[str, Any]:
    """An account shaped like a live one: USD cents plus the same money rendered."""
    return {
        "id": account_id,
        "balances": [
            {
                "key": f"{account_id}/held",
                "amount": 0.0,
                "formatted_local_amount": "$0.00",
            },
            {
                "key": f"{account_id}/available",
                "amount": usd_cents,
                "formatted_local_amount": local,
            },
        ],
        "enrollment": {
            "benefit": {
                "id": "benefit_x",
                "name": "Wellness",
                "benefit_type": "PERK",
            }
        },
    }


# -- the derivations -------------------------------------------------------


def test_the_local_currency_is_derived_from_the_rate_the_balance_was_rendered_at() -> (
    None
):
    """$500.00 shown as $625.00 is a rate of 1.25 — one currency in the list publishes that."""
    found = cli_profile._local_currency(
        [_account("account_1", 50000.0, "$625.00")], CURRENCIES
    )

    assert found["code"] == "XND"
    assert found["confidence"] == "derived"
    assert found["rate_per_usd"] == pytest.approx(1.25, abs=1e-4)


def test_the_largest_balance_decides_because_rounding_hurts_a_small_one() -> None:
    """A $0.07 pot rendered to the cent pins the rate to about 10%; a $500 one to 0.001%."""
    found = cli_profile._local_currency(
        [
            _account("account_1", 7.0, "$0.10"),
            _account("account_2", 50000.0, "$625.00"),
        ],
        CURRENCIES,
    )

    assert found["code"] == "XND"


def test_two_currencies_at_the_same_rate_are_reported_as_ambiguous() -> None:
    """Saying "probably" here would put a wrong currency into a claim."""
    twins = [
        *CURRENCIES,
        {
            "id": "crcy_0000000000000000000004",
            "name": "Eastern dollar",
            "code": "XED",
            "symbol": "$",
            "decimals": 2,
            "latest_rate_amount": 1.2501,
        },
    ]

    found = cli_profile._local_currency(
        [_account("account_1", 50000.0, "$625.00")], twins
    )

    assert found["code"] is None
    assert found["confidence"] == "ambiguous"
    assert found["candidates"] == ["XED", "XND"]


def test_a_currency_pegged_to_the_dollar_still_reads_as_usd() -> None:
    pegged = [
        *CURRENCIES,
        {
            "id": "crcy_0000000000000000000005",
            "name": "Example balboa",
            "code": "XPB",
            "symbol": "B/.",
            "decimals": 2,
            "latest_rate_amount": 1.0,
        },
    ]

    found = cli_profile._local_currency(
        [_account("account_1", 40000.0, "$400.00")], pegged
    )

    assert found["code"] == "USD"


def test_an_account_with_nothing_in_it_admits_the_currency_is_unknown() -> None:
    """Every balance zero means there is no ratio to take — not a licence to guess."""
    found = cli_profile._local_currency(
        [_account("account_1", 0.0, "$0.00")], CURRENCIES
    )

    assert found == {"code": None, "confidence": "unknown"}


def test_a_rate_no_published_currency_matches_is_not_an_empty_account() -> None:
    """Zero candidates is `no_match`, never `ambiguous` with an empty list.

    The text output branches on this field, and reading an empty candidate list
    as "every balance is zero" tells a user holding $500 that they hold nothing.
    """
    found = cli_profile._local_currency(
        [_account("account_1", 50000.0, "$1,000.00")], CURRENCIES
    )

    assert found["code"] is None
    assert found["confidence"] == "no_match"
    assert found["candidates"] == []
    assert found["rate_per_usd"] == pytest.approx(2.0, abs=1e-4)


# A currency with three minor digits — KWD, BHD, OMR, JOD and TND are the real
# ones. Benepass renders them with three digits after the decimal point, which
# is exactly the shape the ordinary thousands rule reads the other way.
THREE_DIGIT_CURRENCIES = [
    *CURRENCIES,
    {
        "id": "crcy_0000000000000000000006",
        "name": "Example three-digit dinar",
        "code": "XKD",
        "symbol": "KD",
        "decimals": 3,
        "latest_rate_amount": 0.3075,
    },
]


def test_a_three_minor_digit_currency_is_not_read_as_a_thousands_mark() -> None:
    """`KD153.750` is 153.75, not 153,750.

    Read the ordinary way it derives a rate exactly 1000x the published one and
    matches nothing, so an account held in one of these five currencies could
    never be named — deterministically, not as a rounding accident.
    """
    found = cli_profile._local_currency(
        [_account("account_1", 50000.0, "KD153.750")], THREE_DIGIT_CURRENCIES
    )

    assert found["code"] == "XKD"
    assert found["confidence"] == "derived"
    assert found["rate_per_usd"] == pytest.approx(0.3075, abs=1e-4)


def test_the_same_rendering_with_both_separators_is_unambiguous() -> None:
    """The ambiguity is only ever in a LONE separator: `KD1,537.500` reads one way."""
    found = cli_profile._local_currency(
        [_account("account_1", 500000.0, "KD1,537.500")], THREE_DIGIT_CURRENCIES
    )

    assert found["code"] == "XKD"
    assert found["rate_per_usd"] == pytest.approx(0.3075, abs=1e-4)


def test_the_second_reading_cannot_rescue_a_two_decimal_currency() -> None:
    """Only a 3-decimal currency may claim the decimal-point reading.

    `4700.000` read as 4,700.000 divides to exactly the krona's published rate
    — but the krona has two minor digits, so Benepass would never have rendered
    it that way, and adopting it would trade a legible `no_match` for a
    confident wrong currency.
    """
    found = cli_profile._local_currency(
        [_account("account_1", 50000.0, "4700.000")], THREE_DIGIT_CURRENCIES
    )

    assert found["code"] is None
    assert found["confidence"] == "no_match"
    assert found["rate_per_usd"] == pytest.approx(9400.0, abs=1e-2)


def test_a_negative_balance_does_not_make_every_currency_a_candidate() -> None:
    """A signed ratio passes the `<= tolerance` test against every published rate."""
    found = cli_profile._local_currency(
        [_account("account_1", -50000.0, "-$625.00")], CURRENCIES
    )

    assert found["code"] == "XND"
    assert found["confidence"] == "derived"


def _events(dates: list[str], kind: str, amount: int | None = 7500) -> list[dict]:
    return [
        {"event_type": kind, "reference_time": f"{d}T00:00:00Z", "amount": amount}
        for d in dates
    ]


def test_a_monthly_top_up_is_named_from_the_gaps_between_events() -> None:
    schedule = cli_profile._schedule(
        _events(
            ["2030-01-15", "2030-02-15", "2030-03-15", "2030-04-15"], "contribution"
        )
        + _events(["2030-01-14", "2030-02-14"], "expiration", -4400),
        7500,
    )

    assert schedule["cadence"] == "monthly"
    assert schedule["next_top_up"] == {"date": "2030-01-15", "amount_usd": "$75.00"}
    assert schedule["next_expiry"]["date"] == "2030-01-14"
    assert schedule["rolls_over_usd"] == "$75.00"


def test_the_next_expiry_carries_a_date_and_no_exposure_figure() -> None:
    """The event's own amount is the number nothing should ever quote.

    Benepass computes it from the balance as it stands today, applying none of
    the contributions scheduled before the expiry, so a pot about to forfeit
    its whole monthly top-up reports $0.00 forever. `expiring` projects the
    schedule and reports the honest figure; publishing the unprojected one here
    unwarned is how it ends up in front of a user.
    """
    schedule = cli_profile._schedule(_events(["2030-02-14"], "expiration", 0), 7500)

    assert schedule["next_expiry"] == {"date": "2030-02-14"}


def test_one_top_up_in_the_horizon_reports_what_was_seen_not_a_guessed_cadence() -> (
    None
):
    """Two points are the minimum an interval can be measured from."""
    schedule = cli_profile._schedule(_events(["2030-06-01"], "contribution", 120000), 0)

    assert schedule["cadence"] == f"once in {cli_profile.HORIZON_DAYS}d"
    assert schedule["contributions_in_horizon"] == 1


def test_two_events_on_one_day_sort_even_when_one_amount_is_missing() -> None:
    """Benepass reports a null event amount on some annual pots.

    These are (date, amount) tuples, so a plain sort falls through to comparing
    None with an int the moment two events share a date — and `profile` is the
    first command both /benepass:setup and /benepass:backfill run.
    """
    schedule = cli_profile._schedule(
        [
            {
                "event_type": "expiration",
                "reference_time": "2030-06-30T00:00:00Z",
                "amount": None,
            },
            {
                "event_type": "expiration",
                "reference_time": "2030-06-30T00:00:00Z",
                "amount": -4400,
            },
            {
                "event_type": "contribution",
                "reference_time": "2030-01-15T00:00:00Z",
                "amount": None,
            },
            {
                "event_type": "contribution",
                "reference_time": "2030-01-15T00:00:00Z",
                "amount": 7500,
            },
        ],
        0,
    )

    assert schedule["next_expiry"]["date"] == "2030-06-30"
    assert schedule["next_top_up"]["date"] == "2030-01-15"


def test_a_one_off_pot_with_no_schedule_says_none_rather_than_nothing() -> None:
    schedule = cli_profile._schedule([], None)

    assert schedule["cadence"] == "none scheduled"
    assert schedule["next_top_up"] is None
    assert schedule["next_expiry"] is None


def test_the_claim_window_is_read_off_the_benefits_own_validations() -> None:
    window, requires = cli_profile._claim_window(
        [
            {
                "item_type": "purchase_date",
                "required": True,
                "substantiation_format": {
                    "validations": [
                        {
                            "key": "date_gte",
                            "rule": {
                                "metadata": {
                                    "frontend_validation_value": "2025-01-01T00:00:00Z"
                                }
                            },
                        },
                        {
                            "key": "date_lte",
                            "rule": {
                                "metadata": {
                                    "frontend_validation_value": "2099-12-31T00:00:00Z"
                                }
                            },
                        },
                    ]
                },
            },
            {"item_type": "note", "required": True, "substantiation_format": {}},
            {"item_type": "receipt", "required": True, "substantiation_format": {}},
        ]
    )

    assert window["from"] == "2025-01-01"
    assert window["to"] == "2099-12-31"
    # date_lte is "now" on every live benefit seen, so a future bound must not
    # be presented as a deadline someone has to meet.
    assert window["to_is_today"] is True
    assert requires == {"purchase_date": True, "note": True, "receipt": True}


def test_a_benefit_with_no_date_gte_has_no_lower_bound_at_all() -> None:
    """CLAIMS FROM `-` means "any purchase date", not "unknown" — the backfill
    floor is derived from this column, and reading `-` as a missing answer
    either stalls the window or invents one."""
    window, requires = cli_profile._claim_window(
        [{"item_type": "receipt", "required": True, "substantiation_format": {}}]
    )

    assert window["from"] is None
    assert cli_profile._window_cell(window) == "-"


# -- the command -----------------------------------------------------------


USER = {
    "first_name": "Example",
    "last_name": "Person",
    "full_name": "Example Person",
    "preferred_first_name": "Ex",
    "preferred_locale": "en-US",
    "email": "you@example.org",
    "personal_email": "you@example.com",
    "date_of_birth": "1990-01-01T00:00:00Z",
    "phone_number": "+00 000 000 000",
    # The employer's registered address, which is what a multinational account
    # really carries here — nowhere near where this employee lives.
    "legal_address": {
        "line1": "1 Example Plaza",
        "city": "Exampleville",
        "state": "EX",
        "country": {"name": "United States", "abbreviation": "US"},
    },
    "country": {"name": "Example Republic", "abbreviation": "XR"},
}

WORKSPACES = [
    {"id": "workspace_personal", "type": "user", "active_accounts": 0},
    {
        "id": "workspace_employment",
        "type": "employment",
        "active_accounts": 1,
        "employment": {
            "hire_date": "2026-05-19",
            "status": "active",
            "employer": {"id": "employer_1", "name": "Example Corp"},
            "country": {"name": "Example Republic", "abbreviation": "XR"},
        },
    },
]


class FakeClient:
    workspace_id = "workspace_employment"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/v2/me/":
            return USER
        if path.endswith("/eligibility-categories/"):
            return {"data": [{"id": "cat_1", "name": "Transit"}]}
        return {"data": []}

    def workspaces(self) -> list[dict[str, Any]]:
        return WORKSPACES

    def accounts(self) -> list[dict[str, Any]]:
        return [_account("account_1", 50000.0, "$625.00")]

    def currencies(self) -> list[dict[str, Any]]:
        return CURRENCIES

    def substantiation(self, benefit_id: str) -> Any:
        return {
            "data": [
                {"item_type": "note", "required": True, "substantiation_format": {}}
            ]
        }

    def next_events(self, account_id: str, until: str) -> list[dict[str, Any]]:
        return _events(["2030-01-15", "2030-02-15"], "contribution")

    def max_rollover(self, account_id: str) -> dict[str, Any]:
        return {"max_rollover_amount": 7500}


@pytest.fixture(autouse=True)
def client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_profile, "run", lambda fn, *a, **k: fn(FakeClient()))
    monkeypatch.setattr(session, "email", lambda: "you@example.com")


def test_the_address_on_file_is_never_reported_as_where_the_user_lives() -> None:
    """It is the employer's address; an interview reading it would ask the wrong questions."""
    result = runner.invoke(app, ["profile"])

    assert result.exit_code == 0
    assert "Exampleville" not in result.stdout
    assert "1 Example Plaza" not in result.stdout
    assert "not exposed by the API" in result.stdout


def test_neither_is_anything_an_agent_session_has_no_use_for() -> None:
    """Every command an agent runs is archived; a date of birth has no decision behind it."""
    result = runner.invoke(app, ["profile", "--json"])

    assert "1990-01-01" not in result.stdout
    assert "+00 000 000 000" not in result.stdout


def test_the_json_carries_what_a_setup_interview_needs() -> None:
    result = runner.invoke(app, ["profile", "--json"])

    data = json.loads(result.stdout)
    assert data["login_email"] == "you@example.com"
    assert data["country"] == {"name": "Example Republic", "code": "XR"}
    assert data["city"] is None and data["timezone"] is None
    assert data["workspace"]["employer"] == "Example Corp"
    assert data["workspace"]["id"] == "workspace_employment"
    assert data["other_workspaces"] == [
        {"id": "workspace_personal", "type": "user", "active_accounts": 0}
    ]
    assert data["local_currency"]["code"] == "XND"

    benefit = data["benefits"][0]
    assert benefit["category_count"] == 1
    assert benefit["requires"] == {"note": True}
    assert benefit["cadence"] == "monthly"
    assert benefit["available_usd"] == "$500.00"
    assert benefit["available_local"] == "$625.00"


def test_the_text_output_says_what_an_empty_claims_from_means() -> None:
    """The fake account states no `date_gte`, which is the case a reader of the
    table has to be able to tell from a field the CLI failed to read."""
    result = runner.invoke(app, ["profile"])

    assert "no lower bound at all" in result.stdout


def test_a_benefit_that_demands_a_note_is_called_out_in_the_text_output() -> None:
    result = runner.invoke(app, ["profile"])

    assert "Requires a note on every claim: Wellness." in result.stdout
