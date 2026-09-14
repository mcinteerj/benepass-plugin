"""`benepass match` — is this purchase already on file? The double-dip check.

Before filing a receipt, the question that matters is whether the benefit card
already paid for it, or whether the same receipt was claimed once before. The
card network's descriptor rarely resembles the merchant's own name — a transit
tap arrives as an authority code, a rideshare as a support URL — so searching by
merchant name misses exactly the rows that matter. Amount and date do not lie,
and this command searches on those.

The currency on a row does lie, though, which is why `--currency` only marks
rows and never removes them — see the `match` docstring.

Registered onto the shared typer app by `register(app)`.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Any

import typer

from . import api, output
from .output import emit_json
from .runner import fail, run

# Rows that represent money moving for a purchase. Employer contributions and
# expirations are the pot filling and draining — they have no merchant amount
# and can never be the purchase someone is about to claim.
PURCHASE_TYPES = ("card", "reimbursement")

# The API caps a page at 100 and reports total_count, so paging can be exact
# rather than a guess — see _fetch_rows for what happens when it does not. The
# ceiling is a runaway guard, not an expected limit.
PAGE_SIZE = 100
MAX_PAGES = 20

DEFAULT_SCAN_DAYS = 400
DEFAULT_WINDOW_DAYS = 3

# Merchant amounts arrive in minor units, and the only scale ever observed live
# is a hundredth: a rupiah amount arrives as -98765400 for a figure its own
# rendering prints as 987.654,00. That is one two-minor-digit currency, though,
# and it does not settle what Benepass does with a currency it records in major
# units (JPY, KRW) or with three minor digits (KWD, BHD, OMR, JOD, TND). Getting
# that wrong is 100x or 10x out on the one command whose "no" is meant to be
# trustworthy, so the scale is read off the row rather than assumed: every row
# carries the same money already rendered in `formatted_merchant_amount`, and
# the scale that reproduces the printed figure is the scale that row used.
# x100 is tried first — it is the observed one — and stands as the fallback
# where a row carries no rendering, or where rounding leaves none of these
# reproducing it.
MINOR_UNITS = 100.0
CANDIDATE_SCALES = (100.0, 1.0, 1000.0)

# Half a cent: two renderings of the same purchase can differ by a rounding
# step, and nothing legitimate differs by less than this without being the same
# amount.
AMOUNT_TOLERANCE = 0.005


def _scan_since(target: date | None, window: int, days: int) -> str:
    """Lower bound for the server-side fetch.

    `since`/`until` filter `transaction_time`, which for a reimbursement is the
    day the claim was FILED — often months after the purchase it is for. A
    claim can never be filed before the purchase happened, so the lower bound
    is safe for both row types.
    """
    if target is not None:
        return (target - timedelta(days=window)).isoformat()
    return (date.today() - timedelta(days=days)).isoformat()


def _fetch_rows(
    client: api.Client,
    since: str,
    until: str | None = None,
    kind: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Every transaction in the server-side filter, paged. Returns (rows, truncated).

    What ends the scan is `total_count`, or a page that adds nothing new —
    never a page merely arriving shorter than it was asked for. This is an
    unofficial API that caps and silently ignores parameters it does not like,
    so a short page is the server's choice, not proof that nothing follows it;
    reading it as the end is how this command would print a confident "no
    matches" over a scan that stopped before the matching row. Offsets advance
    by what has actually been collected rather than by a fixed stride, so a
    short page cannot skip rows either.

    Rows are deduplicated by id as they arrive, which is what stops a server
    that ignores `offset` filling the list with twenty copies of page one. That
    ends the scan — but it ends it as INCOMPLETE, not as a "no": a page that
    carried rows and none of them new is the server handing back something it
    has already given us, so whatever sits past where it stopped was never
    fetched. An EMPTY page is the other thing entirely — the server saying
    there is nothing at this offset, which is the ordinary end of the data.

    `total_count` is read only as a reason to STOP, never as a reason to call
    an empty page incomplete: it is the count the server chose to report for a
    filtered query, and reading a larger number as "rows remain" would mark
    every dedup scan unusable if it were ever computed over unfiltered rows.
    """
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        payload = client.transactions(
            limit=PAGE_SIZE,
            offset=len(collected),
            since=since,
            until=until,
            kind=kind,
        )
        page = api.rows(payload)
        fresh = 0
        for row in page:
            row_id = str(row.get("id") or "")
            if row_id and row_id in seen:
                continue
            if row_id:
                seen.add(row_id)
            collected.append(row)
            fresh += 1
        if fresh == 0:
            return collected, bool(page)
        total = payload.get("total_count") if isinstance(payload, dict) else None
        if isinstance(total, int) and len(collected) >= total:
            return collected, False
    return collected, True


def _collect(
    client: api.Client, since: str, target: date | None, window: int
) -> tuple[list[dict[str, Any]], bool]:
    """The rows to search, in two passes when a date is given. Returns (rows, truncated).

    **Reimbursements are never bounded above**, because a claim filed after the
    searched window is precisely the late-filed duplicate this command exists
    to catch. **Card rows are**, because a card row's `transaction_time` IS its
    purchase time, so nothing settled well after the window can be the purchase
    in hand — and leaving that pass open-ended is what made an old date on a
    busy account page through years of history and hit the ceiling below, which
    would turn a truncated scan into a "no".

    A day of slack on the upper bound absorbs the timezone shift between a
    swipe's UTC timestamp and the local date on the receipt. The two passes are
    merged by id, so nothing is double-counted if Benepass ever ignores the
    `type` filter (it ignores any parameter it does not recognise, silently).
    """
    if target is None:
        return _fetch_rows(client, since)

    until = (target + timedelta(days=window + 1)).isoformat()
    cards, cards_truncated = _fetch_rows(client, since, until=until, kind="card")
    claims, claims_truncated = _fetch_rows(client, since, kind="reimbursement")

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in (*cards, *claims):
        row_id = str(row.get("id") or "")
        if row_id and row_id in seen:
            continue
        if row_id:
            seen.add(row_id)
        merged.append(row)
    return merged, (cards_truncated or claims_truncated)


def _truncation_notice(target: date | None) -> str:
    """Said the same way on stdout and stderr: a capped scan is not a "no"."""
    advice = (
        "narrow it with a tighter --window (--currency will not help — it flags "
        "rows, it does not drop them), or check by hand with "
        "`benepass transactions --type card --since … --until …` around the purchase "
        "date AND `benepass transactions --type reimbursement --since <purchase date>` "
        "with NO upper bound — a claim's filing time is not its purchase date, so an "
        "--until would hide the late-filed duplicate; read the PURCHASED column"
        if target is not None
        else "narrow it with --date, or a smaller --days"
    )
    return (
        f"SCAN INCOMPLETE — the scan never reached the end of the data: it either hit "
        f"the {MAX_PAGES * PAGE_SIZE}-transaction ceiling or the server stopped handing "
        f"over new rows before it. An empty result here is NOT a 'no': a matching row "
        f"may sit past where it stopped. Treat it as unknown and {advice}."
    )


def _printed_amount(txn: dict[str, Any]) -> float | None:
    """The figure the row's own rendering prints, as a number.

    `money_readings` hands back the thousands reading first and the decimal
    reading second where a string is genuinely both — `KD153.750` is 153750 by
    the ordinary rule and 153.75 in a currency with three minor digits — and
    asks the caller for a second source of truth. The row states its currency,
    which is that source: the three-minor-digit codes take the decimal reading,
    everything else the likeliest one.
    """
    readings = output.money_readings(txn.get("formatted_merchant_amount"))
    if not readings:
        return None
    if output.row_ccy(txn) in output.THREE_MINOR_DIGIT_CCYS:
        return readings[-1]
    return readings[0]


def _merchant_amount(txn: dict[str, Any]) -> float | None:
    """The receipt's own figure as a positive number, in the merchant currency.

    Scaled by whichever of CANDIDATE_SCALES reproduces what the row prints —
    see that constant for why the scale is not assumed.
    """
    raw = txn.get("merchant_amount")
    if raw is None:
        return None
    try:
        minor = abs(float(raw))
    except (TypeError, ValueError):
        return None
    printed = _printed_amount(txn)
    if printed:
        for scale in CANDIDATE_SCALES:
            if abs(minor / scale - printed) <= AMOUNT_TOLERANCE:
                return minor / scale
    return minor / MINOR_UNITS


def _merchant_name(txn: dict[str, Any]) -> str | None:
    name = txn.get("merchant_name") or txn.get("title")
    return str(name) if name else None


def _benefit_name(txn: dict[str, Any]) -> str | None:
    account = txn.get("account")
    if not isinstance(account, dict):
        return None
    benefit = (account.get("enrollment") or {}).get("benefit") or {}
    name = benefit.get("name")
    return str(name) if name else None


def _account_id(txn: dict[str, Any]) -> str | None:
    account = txn.get("account")
    if isinstance(account, dict):
        return str(account.get("id")) if account.get("id") else None
    return str(account) if account else None


def register(app: typer.Typer) -> None:
    @app.command()
    def match(
        amount: float = typer.Option(
            ...,
            "--amount",
            help="The receipt's own figure, positive, e.g. 42.50.",
        ),
        currency: str = typer.Option(
            None,
            "--currency",
            help=(
                "The receipt's ISO code (e.g. GBP). ADVISORY — rows in another "
                "currency are flagged `ccy≠`, never dropped."
            ),
        ),
        date_opt: str = typer.Option(
            None,
            "--date",
            help="Purchase date, YYYY-MM-DD. Without it, amount alone is matched.",
        ),
        window: int = typer.Option(
            DEFAULT_WINDOW_DAYS,
            "--window",
            help="Days either side of --date to accept (default 3).",
        ),
        days: int = typer.Option(
            DEFAULT_SCAN_DAYS,
            "--days",
            help=f"How far back to scan when --date is absent (default {DEFAULT_SCAN_DAYS}).",
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """Find transactions already on file for an amount (and optionally a date).

        The anti-double-dip check: run it before filing a receipt. A card row at
        the same amount and date means the benefit card already paid, and filing
        a reimbursement for it would claim the same money twice; a reimbursement
        row means this receipt has been claimed before.

        Both row types are searched, in the MERCHANT's currency — the figure
        printed on the receipt, not Benepass's USD ledger figure or its
        conversion into the local currency.

        `--currency` is ADVISORY and never excludes a row. It marks the ones
        whose merchant currency is not the code you passed — `ccy≠` in the
        table, `"currency_mismatch": true` in the JSON — and leaves them in the
        answer. Benepass's own label for a row is not trustworthy: a card row
        has been seen carrying a currency the purchase was not made in, and a
        filter on that code answered "No matches" for a charge sitting in the
        list, which is the one failure this command must never have. Two currencies sharing
        a symbol is the case the flag exists for, and a flag tells them apart
        without hiding anything.

        Exits 0 whether or not anything matched; an empty result is an answer,
        not a failure.
        """
        if amount <= 0:
            fail("--amount must be a positive number — pass the receipt's figure")
        if window < 0:
            fail("--window cannot be negative")
        if days < 1:
            fail("--days must be at least 1")

        target: date | None = None
        if date_opt:
            iso = output.parse_date(date_opt)
            # Only ISO here, unlike `submit`, which has a human confirming its
            # preview. A silently misread 03/09 would answer "no matches" for a
            # purchase that IS on file, and the whole point of this command is
            # that its "no" can be trusted.
            if not iso or iso != date_opt.strip():
                fail(f"--date must be YYYY-MM-DD, got {date_opt!r}")
            target = date.fromisoformat(iso)

        wanted_ccy = currency.strip().upper() if currency else None
        since = _scan_since(target, window, days)

        def _search(client: api.Client) -> dict[str, Any]:
            rows, truncated = _collect(client, since, target, window)

            # Amount and currency first, because deciding the date of a
            # reimbursement costs one API call per row: the list endpoint
            # carries no claim data, and the filing date is not the purchase
            # date. Filtering first turns that into a handful of fetches.
            # Amount alone selects the candidates. `--currency` is applied
            # afterwards as a FLAG, never as a filter: Benepass has labelled a
            # card row with a currency the purchase was not made in, and an
            # unstated one (CCY `LOCAL`) carries no label at all, so excluding
            # on the code answers "No matches" for a row that is on file. That
            # false "no" is a double-dip filed twice; a flagged extra row costs
            # a second of reading.
            candidates = [
                txn
                for txn in rows
                if txn.get("transaction_type") in PURCHASE_TYPES
                and (found := _merchant_amount(txn)) is not None
                and abs(found - amount) <= AMOUNT_TOLERANCE
            ]

            found_rows: list[dict[str, Any]] = []
            for txn in candidates:
                estimated = False
                if txn.get("transaction_type") == "card":
                    when = output.parse_date(txn.get("transaction_time"))
                else:
                    detail = client.transaction(str(txn.get("id")))
                    when = output.claim_purchase_date(detail)
                    if when is None:
                        # No purchase date on the claim: fall back to the filing
                        # date and say so, rather than dropping the row. A
                        # missed duplicate costs real money; a row shown with a
                        # caveat costs a second of reading.
                        when = output.parse_date(txn.get("transaction_time"))
                        estimated = True
                # The window is applied only to rows whose purchase date is
                # actually KNOWN. A claim that carried no purchase date, or a
                # row whose timestamp will not parse, has an unknown date — and
                # unknown is not "not that one", the same reason a row with no
                # stated merchant currency is kept above. Filtering those on the
                # filing date instead would drop exactly the late-filed
                # duplicate this command exists to catch, and answer "No
                # matches" for a receipt that is already on file.
                known_date = when is not None and not estimated
                if (
                    target is not None
                    and known_date
                    and abs((date.fromisoformat(str(when)) - target).days) > window
                ):
                    continue
                found_rows.append(
                    {
                        "type": str(txn.get("transaction_type") or "-"),
                        "id": str(txn.get("id") or "-"),
                        "merchant": _merchant_name(txn),
                        "amount": _merchant_amount(txn),
                        "formatted_amount": output.row_amount(txn),
                        "currency": output.row_ccy(txn),
                        "currency_mismatch": (
                            wanted_ccy is not None and output.row_ccy(txn) != wanted_ccy
                        ),
                        "date": when,
                        "date_is_filing_date": estimated,
                        "benefit": _benefit_name(txn),
                        "account_id": _account_id(txn),
                        "status": str(txn.get("transaction_status") or "-"),
                    }
                )

            missing = [r for r in found_rows if not r["benefit"] and r["account_id"]]
            if missing:
                # The list rows normally embed the benefit, so this is one extra
                # call only when Benepass has left it off.
                names = {}
                for acct in client.accounts():
                    name = ((acct.get("enrollment") or {}).get("benefit") or {}).get(
                        "name"
                    )
                    if acct.get("id") and name:
                        names[str(acct["id"])] = str(name)
                for row in missing:
                    row["benefit"] = names.get(str(row["account_id"]))
            return {
                "truncated": truncated,
                "matches": sorted(
                    found_rows, key=lambda r: str(r["date"] or ""), reverse=True
                ),
            }

        result = run(_search)
        matches: list[dict[str, Any]] = result["matches"]
        truncated: bool = result["truncated"]

        if truncated:
            print(f"benepass: {_truncation_notice(target)}", file=sys.stderr)

        if as_json:
            # An envelope, not a bare list: the caller filing a claim off this
            # answer has to be able to tell "nothing matched" from "the scan
            # never finished", and a list cannot carry that.
            emit_json(result)
            return

        if truncated:
            print(_truncation_notice(target))

        scope = (
            f"±{window} day(s) around {target.isoformat()}, scanning from {since}"
            if target
            else f"the last {days} days (from {since}), any date"
        )
        if not matches:
            print(f"No matches for {amount:,.2f} — searched {scope}.")
            if wanted_ccy:
                print(
                    f"--currency {wanted_ccy} did not narrow this: every currency was "
                    "searched and\nnothing matched the amount."
                )
            return
        print(
            output.table(
                [
                    [
                        row["type"],
                        row["id"],
                        str(row["merchant"] or "-")[:28],
                        str(row["formatted_amount"]),
                        str(row["currency"]),
                        ("~" if row["date_is_filing_date"] else "")
                        + str(row["date"] or "-"),
                        str(row["benefit"] or "-")[:34],
                        row["status"],
                        "ccy≠" if row["currency_mismatch"] else "",
                    ]
                    for row in matches
                ],
                [
                    "TYPE",
                    "ID",
                    "MERCHANT",
                    "AMOUNT",
                    "CCY",
                    "DATE",
                    "BENEFIT",
                    "STATUS",
                    "FLAGS",
                ],
            )
        )
        mismatched = sum(1 for row in matches if row["currency_mismatch"])
        ccy_advisory = (
            f"\n{mismatched} row(s) are flagged `ccy≠`: their merchant currency is not "
            f"{wanted_ccy}, or Benepass\nstated none for them (CCY `{output.LOCAL_CCY}`). "
            "--currency is ADVISORY — it marks rows and never\ndrops them, because Benepass's "
            "own label can be wrong: a card row has arrived labelled with a\ncurrency the "
            "purchase was not made in. "
            "Check the amount, the date and the merchant before dismissing one."
            if mismatched and wanted_ccy
            else ""
        )
        print(
            f"\n{len(matches)} match(es) for {amount:,.2f} — searched {scope}."
            "\nDATE is the purchase date: a card row's transaction time, a claim's receipt date."
            "\nA `~` date is the FILING date, shown because that claim carried no purchase date;"
            "\na `-` date means the row stated none at all. Both are listed whatever --date says,"
            "\nbecause an unknown purchase date is not proof this is a different purchase."
            "\nA `card` row means the benefit card already paid — claiming it again is a double-dip."
            "\nA `reimbursement` row means this receipt has been filed before."
            f"{ccy_advisory}"
        )
