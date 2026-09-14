"""`benepass profile` — the account facts an onboarding interview should not ask for.

Who this account is, which employer and workspace it belongs to, which currency
Benepass renders money in, and for every benefit: how much, how many categories,
what date range it accepts receipts for, when it tops up, how much rolls over,
and when the next expiry falls. All of it read from the account, so a setup
conversation can start from what is true rather than from questions.

What the API does NOT expose is worth knowing too, because guessing it wrongly
is worse than asking:

- **No timezone, anywhere.** No `/v2/me*` endpoint carries one.
- **No home address.** `/v2/me/` has `legal_address`, and on a multinational
  employer it is the EMPLOYER's registered address, not where the employee
  lives — a real account reads a head-office city on one continent for an
  employee resident on another.
  It is deliberately not reported here; city and commute have to be asked.
- **No currency field.** Benepass states a local currency nowhere; it only
  renders into it. It is derived here, and the derivation says how sure it is.

Registered onto the shared typer app by `register(app)`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import typer

from . import api, catalog, output, session
from .output import emit_json
from .runner import list_benefits, run

# Far enough ahead to see an annual benefit's next top-up and expiry on any
# plan year, without paying for events nobody will act on.
HORIZON_DAYS = 400

# The rate that produced a rendered local amount is Benepass's own published
# rate, so a match is near-exact; a percent of slack would catch a second
# currency, a tenth of a percent catches only the right one.
RATE_TOLERANCE = 0.001

CADENCES = (
    (6, 8, "weekly"),
    (13, 16, "fortnightly"),
    (26, 32, "monthly"),
    (58, 64, "2-monthly"),
    (85, 96, "quarterly"),
    (175, 190, "6-monthly"),
    (355, 380, "annual"),
)


def _cadence(gaps: list[int]) -> str | None:
    """Name the contribution interval, or None when the gaps fit no pattern."""
    if not gaps:
        return None
    middle = sorted(gaps)[len(gaps) // 2]
    for low, high, name in CADENCES:
        if low <= middle <= high:
            return name
    return f"every ~{middle}d"


def _rate_candidates(
    currencies: list[dict[str, Any]], rate: float
) -> list[dict[str, Any]]:
    """Published currencies whose rate is within tolerance of `rate`."""
    candidates = [
        row
        for row in currencies
        if row.get("latest_rate_amount")
        and abs(float(row["latest_rate_amount"]) - rate) / rate <= RATE_TOLERANCE
    ]
    # A currency pegged 1:1 to USD is indistinguishable from USD by rate alone,
    # and USD is the overwhelmingly likelier reading of a ratio of 1.
    if len(candidates) > 1:
        usd_row = [c for c in candidates if str(c.get("code")).upper() == "USD"]
        if usd_row and abs(rate - 1.0) <= RATE_TOLERANCE:
            return usd_row
    return candidates


def _local_currency(
    accounts: list[dict[str, Any]], currencies: list[dict[str, Any]]
) -> dict[str, Any]:
    """Which currency Benepass renders this account's money in.

    Nothing in the API names it, so it is derived: every balance carries both
    the USD ledger cents and the same money rendered locally, and the ratio of
    those two IS the conversion rate Benepass used. The currency list publishes
    that rate per currency, so the ratio identifies the currency — to within a
    tenth of a percent, which on live data leaves exactly one candidate.

    The largest balance is used because rounding in a small one is a larger
    fraction of it, and its MAGNITUDE is taken: a benefit can carry a negative
    available balance, and a signed ratio would make the tolerance test below
    true for every published rate at once.

    **A three-minor-digit currency is read twice.** `KD153.750` is `153750`
    under the ordinary thousands rule and `153.75` as a decimal point, and only
    the second is right for the five currencies (KWD, BHD, OMR, JOD, TND) that
    have three minor digits — the first derives a rate exactly 1000× the
    published one and matches nothing. So where `money_readings` offers a
    second reading, it is tried against those currencies alone, and used only
    if it identifies exactly one. Anything else keeps the ordinary reading's
    answer, including its `no_match`.

    Four confidences, and the caller should treat anything but the first as
    "ask the user": `derived` (one candidate matched), `ambiguous` (several
    did, and their codes are listed), `no_match` (a rate was readable but no
    published currency is within tolerance of it — a rounded rendering of a
    small balance does this), and `unknown` (every balance was zero, so there
    was no ratio to take at all).
    """
    best: tuple[float, list[float]] | None = None
    for account in accounts:
        balance = output.available_balance(account)
        usd_cents = balance.get("amount")
        readings = [
            v for v in output.money_readings(balance.get("formatted_local_amount")) if v
        ]
        if not usd_cents or not readings:
            continue
        magnitude = abs(float(usd_cents))
        if best is None or magnitude > best[0]:
            best = (magnitude, readings)
    if best is None:
        return {"code": None, "confidence": "unknown"}

    usd_dollars = best[0] / 100
    attempts: list[tuple[float, list[dict[str, Any]]]] = []
    for index, local in enumerate(best[1]):
        # Only the first reading is checked against every currency: the second
        # exists solely because a three-minor-digit rendering is ambiguous, and
        # letting it match anything else would trade a legible `no_match` for a
        # confident wrong answer.
        pool = (
            currencies
            if index == 0
            else [row for row in currencies if output.minor_digits(row) == 3]
        )
        rate = local / usd_dollars
        attempts.append((rate, _rate_candidates(pool, rate)))
        if len(attempts[-1][1]) == 1:
            break
    rate, candidates = next(((r, c) for r, c in attempts if len(c) == 1), attempts[0])
    if len(candidates) != 1:
        return {
            "code": None,
            # Zero candidates is a different answer from several, and saying
            # "ambiguous" with an empty list reads as "every balance is zero"
            # to anything branching on the list rather than on this field.
            "confidence": "ambiguous" if candidates else "no_match",
            "rate_per_usd": round(rate, 6),
            "candidates": sorted(str(c.get("code")) for c in candidates),
        }
    found = candidates[0]
    return {
        "code": str(found.get("code")),
        "name": str(found.get("name")),
        "symbol": str(found.get("symbol") or "").strip() or None,
        "id": str(found.get("id")),
        "decimals": found.get("decimals"),
        "rate_per_usd": round(rate, 6),
        "confidence": "derived",
    }


def _claim_window(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """The purchase dates a benefit will accept, from its substantiation policy.

    `date_gte` is the one that matters and the one that moves: where an employer
    runs a rolling window, a backlog of old receipts stops being claimable all
    at once. `date_lte` is usually just "now" — reported as `today` so nobody
    reads it as a deadline.
    """
    window: dict[str, Any] = {"from": None, "to": None, "to_is_today": False}
    requires: dict[str, bool] = {}

    for row in rows:
        item = str(row.get("item_type") or "")
        if item:
            requires[item] = bool(row.get("required"))
        if item != "purchase_date":
            continue
        fmt = row.get("substantiation_format") or {}
        for validation in fmt.get("validations") or []:
            bound = output.parse_date(
                ((validation.get("rule") or {}).get("metadata") or {}).get(
                    "frontend_validation_value"
                )
            )
            if not bound:
                continue
            if validation.get("key") == "date_gte":
                window["from"] = bound
            elif validation.get("key") == "date_lte":
                window["to"] = bound
    if window["to"] and window["to"] >= date.today().isoformat():
        window["to_is_today"] = True
    return window, requires


def _schedule(events: list[dict[str, Any]], rollover_cents: Any) -> dict[str, Any]:
    """Refresh cadence, next top-up and next expiry, from the account's own schedule."""
    contributions: list[tuple[str, Any]] = []
    expirations: list[tuple[str, Any]] = []
    for event in events:
        when = output.parse_date(
            event.get("reference_time") or event.get("execution_time")
        )
        if not when:
            continue
        if event.get("event_type") == "contribution":
            contributions.append((when, event.get("amount")))
        elif event.get("event_type") == "expiration":
            expirations.append((when, event.get("amount")))
    # Sort on the date alone. These are (date, amount) tuples and Benepass
    # reports a null amount on some annual pots, so a plain sort falls through
    # to comparing None with an int the moment two events share a date.
    contributions.sort(key=lambda item: item[0])
    expirations.sort(key=lambda item: item[0])

    dates = [date.fromisoformat(d) for d, _ in contributions]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    cadence = _cadence(gaps)
    if cadence is None and len(contributions) == 1:
        # One top-up inside a 400-day horizon is yearly at most often; say what
        # was seen rather than inventing a cadence from a single point.
        cadence = f"once in {HORIZON_DAYS}d"
    elif cadence is None:
        cadence = "none scheduled"

    return {
        "cadence": cadence,
        "next_top_up": (
            {"date": contributions[0][0], "amount_usd": output.usd(contributions[0][1])}
            if contributions
            else None
        ),
        # The DATE only, deliberately. The event's own `amount` is Benepass's
        # at-risk figure computed from the balance as it stands today, with
        # none of the contributions scheduled before the expiry applied — so a
        # pot about to forfeit its whole monthly top-up reports $0.00 forever.
        # `benepass expiring` walks the schedule and reports PROJECTED, and
        # that is the only exposure figure anything should quote. Publishing
        # the unprojected one here, unwarned, is how it gets quoted.
        "next_expiry": ({"date": expirations[0][0]} if expirations else None),
        "rolls_over_usd": output.usd(rollover_cents),
        "rolls_over_cents": rollover_cents,
        "contributions_in_horizon": len(contributions),
    }


def register(app: typer.Typer) -> None:
    @app.command()
    def profile(
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """Account facts: who, which employer, which currency, and every benefit's shape.

        The command to run before an onboarding interview, so it asks only what
        the account cannot answer. It reads nothing but `/v2/me*` and writes
        nothing.

        Date of birth, phone number and the address on file are deliberately NOT
        printed: no decision here needs them, and every command an agent runs is
        archived. The address would also mislead — see this module's docstring.
        """

        def _fetch(client: api.Client) -> dict[str, Any]:
            user = client.get("/v2/me/")
            user = user.get("data", user) if isinstance(user, dict) else {}
            workspaces = client.workspaces()
            accounts = client.accounts()
            currencies = client.currencies()

            active = next(
                (w for w in workspaces if str(w.get("id")) == client.workspace_id),
                None,
            )
            employment = (active or {}).get("employment") or {}
            employer = employment.get("employer") or {}
            country = (
                employment.get("country")
                or user.get("country")
                or (user.get("legal_address") or {}).get("country")
                or {}
            )

            until = (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()
            benefits = []
            for benefit in list_benefits(client):
                account_id = benefit["account_id"]
                window, requires = _claim_window(
                    api.rows(client.substantiation(benefit["id"]))
                )
                benefits.append(
                    {
                        "id": benefit["id"],
                        "name": benefit["name"],
                        "benefit_type": benefit["benefit_type"],
                        "account_id": account_id,
                        "available_usd": benefit["available_usd"],
                        "available_local": benefit["available"],
                        "available_cents": benefit["available_cents"],
                        "max_per_expense": benefit["max_per_expense"],
                        # Which currency that cap is in — local on some
                        # enrollments, USD on others, and only the field name
                        # it came from says which.
                        "max_per_expense_ccy": benefit["max_per_expense_ccy"],
                        "category_count": len(
                            catalog.categories_for(client, benefit["id"])
                        ),
                        "claim_window": window,
                        "requires": requires,
                        **_schedule(
                            client.next_events(account_id, until),
                            client.max_rollover(account_id).get("max_rollover_amount"),
                        ),
                    }
                )

            return {
                # The address the login code is emailed to is the one this
                # machine logged in with, which is not necessarily either of the
                # addresses on the record: a Benepass account carries a work
                # address and a personal one, and either can be the login.
                "login_email": session.email()
                or user.get("personal_email")
                or user.get("email"),
                "work_email": user.get("email"),
                "personal_email": user.get("personal_email"),
                "name": user.get("full_name")
                or " ".join(
                    filter(None, [user.get("first_name"), user.get("last_name")])
                )
                or None,
                "preferred_name": user.get("preferred_first_name"),
                "locale": user.get("preferred_locale"),
                "country": {
                    "name": country.get("name"),
                    "code": country.get("abbreviation"),
                },
                # Stated, not omitted: an interview that assumes these are
                # knowable will skip asking for them.
                "city": None,
                "timezone": None,
                "workspace": {
                    "id": client.workspace_id,
                    "type": (active or {}).get("type"),
                    "active_accounts": (active or {}).get("active_accounts"),
                    "employer": employer.get("name"),
                    "employer_id": employer.get("id"),
                    "hire_date": employment.get("hire_date"),
                    "employment_status": employment.get("status"),
                },
                "other_workspaces": [
                    {
                        "id": w.get("id"),
                        "type": w.get("type"),
                        "active_accounts": w.get("active_accounts"),
                    }
                    for w in workspaces
                    if str(w.get("id")) != client.workspace_id
                ],
                "local_currency": _local_currency(accounts, currencies),
                "horizon_days": HORIZON_DAYS,
                "benefits": benefits,
            }

        data = run(_fetch)
        if as_json:
            emit_json(data)
            return

        ccy = data["local_currency"]
        # Branch on the stated confidence, never on whether `candidates` is
        # truthy: an empty candidate list is `no_match`, not "no balances".
        if ccy.get("confidence") == "derived":
            ccy_line = (
                f"{ccy['code']} — {ccy.get('name')} ({ccy.get('symbol') or '?'}), "
                f"{ccy['rate_per_usd']} per USD ({ccy['confidence']})"
            )
        elif ccy.get("confidence") == "ambiguous":
            ccy_line = (
                f"ambiguous at {ccy['rate_per_usd']} per USD — "
                f"one of {', '.join(ccy['candidates'])}"
            )
        elif ccy.get("confidence") == "no_match":
            ccy_line = (
                f"unknown — balances imply {ccy['rate_per_usd']} per USD, which matches "
                "no rate Benepass publishes; ask the user"
            )
        else:
            ccy_line = "unknown — every balance is zero, so no rate could be read"

        workspace = data["workspace"]
        country = data["country"]
        print("Account")
        print(f"  Login email:     {data['login_email'] or '-'}")
        for label, key in (
            ("Work email", "work_email"),
            ("Personal", "personal_email"),
        ):
            if data[key] and data[key] != data["login_email"]:
                print(f"  {label + ':':16} {data[key]}")
        print(f"  Name:            {data['name'] or '-'}")
        print(f"  Employer:        {workspace['employer'] or '-'}")
        print(
            f"  Employment:      {workspace['employment_status'] or '-'}"
            f"{', since ' + workspace['hire_date'] if workspace['hire_date'] else ''}"
        )
        print(
            f"  Country:         {country['name'] or '-'}"
            f"{' (' + country['code'] + ')' if country['code'] else ''}"
        )
        print(f"  Locale:          {data['locale'] or '-'}")
        print(
            f"  Workspace:       {workspace['id'] or '-'}  "
            f"({workspace['type'] or '-'}, {workspace['active_accounts']} active account(s))"
        )
        print(f"  Local currency:  {ccy_line}")
        print("  City, timezone:  not exposed by the API — ask, don't guess")

        print("\nBenefits")
        print(
            output.table(
                [
                    [
                        str(b["id"]),
                        str(b["name"] or "-")[:34],
                        str(b["category_count"]),
                        str(b["available_usd"] or "-"),
                        str(b["available_local"] or "-"),
                        str(b["cadence"] or "-"),
                        (b["next_top_up"] or {}).get("date") or "-",
                        str(b["rolls_over_usd"] or "-"),
                        (b["next_expiry"] or {}).get("date") or "-",
                        _window_cell(b["claim_window"]),
                    ]
                    for b in data["benefits"]
                ],
                [
                    "BENEFIT ID",
                    "NAME",
                    "CATS",
                    "AVAIL USD",
                    "AVAIL LOCAL",
                    "REFRESH",
                    "NEXT TOP-UP",
                    "ROLLS OVER",
                    "NEXT EXPIRY",
                    "CLAIMS FROM",
                ],
            )
        )
        needs_note = [
            str(b["name"])
            for b in data["benefits"]
            if (b["requires"] or {}).get("note")
        ]
        print(
            "\nCATS is the eligible-category count — fewer means narrower, and the claiming rule is"
            "\nto use the narrowest benefit that covers a purchase. ROLLS OVER is how much survives"
            "\nNEXT EXPIRY; anything above it is forfeited. CLAIMS FROM is the oldest purchase date"
            "\nthe benefit accepts, and it can move — re-read it before promising a backlog is safe."
            "\nA `-` there is not a missing answer: that benefit states no lower bound at all, so no"
            "\npurchase is too old for it and it puts no floor under how far back a backfill searches."
            "\nNEXT EXPIRY is a date and nothing else — `benepass expiring` is what says how much is"
            "\nat risk, and its PROJECTED column is the figure to read."
            f"\nSchedule columns look {HORIZON_DAYS} days ahead. AVAIL USD is the ledger; AVAIL LOCAL"
            "\nis the same money converted — don't compare the two."
        )
        if needs_note:
            print(f"Requires a note on every claim: {', '.join(needs_note)}.")


def _window_cell(window: dict[str, Any]) -> str:
    start = window.get("from") or "-"
    if window.get("to") and not window.get("to_is_today"):
        return f"{start} → {window['to']}"
    return str(start)
