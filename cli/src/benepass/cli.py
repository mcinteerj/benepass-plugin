"""benepass — CLI for the Benepass employee benefits platform.

Balances, transactions, categories, and out-of-pocket reimbursement submission.
Login is passwordless: Benepass emails a 6-digit code, which `benepass login`
either collects itself (BENEPASS_OTP_COMMAND, or an interactive prompt) or waits
for you to supply with `benepass login --code <code>`.

Unofficial: the endpoints are the ones Benepass's own web app uses, and they can
change without notice.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import typer

from . import (
    api,
    auth,
    cache,
    cli_catalog,
    cli_match,
    cli_profile,
    cli_submit,
    output,
    session,
)
from .output import emit_json
from .runner import EXIT_LOGIN_REQUIRED, fail, list_benefits, notify, run

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=__doc__,
    context_settings={"help_option_names": ["-h", "--help"]},
    # Typer's rich traceback prints every frame's LOCALS by default on older
    # releases, and the frames that refresh a session hold the refresh token. An
    # unexpected exception must never be the thing that copies it into a log.
    pretty_exceptions_show_locals=False,
)

# Every amount a transaction row shows is paired with a currency code, because
# Benepass renders the same row in the merchant's currency, in USD ledger cents
# and in the employee's local currency, and those are three different numbers.
# output.row_amount/row_ccy pick the pair and are shared with the other commands
# that print a transaction; the reasoning lives in row_amount's docstring.

MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "heic": "image/heic",
    "webp": "image/webp",
}
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@app.command()
def login(
    email: str = typer.Option(
        None, "--email", help="Benepass login email (default: $BENEPASS_EMAIL)."
    ),
    code: str = typer.Option(
        None,
        "--code",
        help="Finish a login started earlier with the code Benepass emailed you.",
    ),
    force: bool = typer.Option(
        False, "-f", "--force", help="Re-login even if a session already exists."
    ),
) -> None:
    """Log in. Requests an emailed 6-digit code, then exchanges it for a session.

    Passing the code in argv is fine — it is single-use and dies in minutes. The
    refresh token it buys is not, and is never accepted or printed as one.
    """
    if code:
        try:
            completed = auth.complete(code)
        except auth.LoginRequired as exc:
            # No challenge to answer, or it went stale: the remedy is a fresh
            # `benepass login`, the same as any other "you need to log in".
            fail(str(exc), EXIT_LOGIN_REQUIRED)
        except api.BenepassError as exc:
            fail(str(exc))
        print(
            f"Logged in as {completed}. Session stored at {session.STATE_FILE} (mode 0600)."
        )
        return

    # $BENEPASS_EMAIL outranks everything on every OTHER command, so a --email
    # that disagrees with it buys a session no later command will use: the login
    # succeeds, and `benefits` then exits 3 naming a mismatch the user was never
    # warned about. Refuse here, where the fix is one unset away, rather than
    # after a one-time code has been spent.
    env_address = os.environ.get("BENEPASS_EMAIL")
    if email and env_address and email != env_address:
        fail(
            f"--email says {email} but BENEPASS_EMAIL says {env_address}, and "
            "the environment variable wins on every other command — this login "
            f"would succeed and then every read would refuse. Unset "
            f"BENEPASS_EMAIL to use {email}, or drop --email to use "
            f"{env_address}."
        )

    address = email or session.email()
    if not address:
        fail(
            "no email — this is the first login on this machine, so pass "
            "--email you@example.com (the Benepass account address) or set "
            "BENEPASS_EMAIL. It is remembered afterwards."
        )
    stored_address = session.load().get("email")
    # Only a token that belongs to the address being asked for counts as "already
    # logged in" — and a stored token can be dead in ways that do not clear it
    # (anything but a 401 or invalid_grant), so name the escape hatch. Compare
    # the RESOLVED address, not the --email option: $BENEPASS_EMAIL names the
    # account just as --email does, and comparing only the option reported the
    # PREVIOUS account as still logged in while answering every later command
    # with its token.
    same_account = address == stored_address
    # $BENEPASS_EMAIL beats the stored address, so an exported leftover quietly
    # sends the code to an account nobody meant to use. Name both rather than
    # letting the user find out from a code that never arrives.
    mismatch = session.account_mismatch()
    if mismatch:
        configured, owner = mismatch
        print(
            f"benepass: BENEPASS_EMAIL is {configured}, but the stored session "
            f"belongs to {owner} — logging in as {configured}.",
            file=sys.stderr,
        )
    if session.refresh_token() and not force and same_account:
        print(
            f"Already logged in as {stored_address or address}. "
            "Use --force to re-login — do that if commands are still failing "
            "with an authentication error."
        )
        return
    try:
        done = auth.login(address, verbose=True)
    except auth.LoginRequired as exc:
        fail(str(exc), EXIT_LOGIN_REQUIRED)
    except api.BenepassError as exc:
        fail(str(exc))
    if done:
        print(
            f"Logged in as {address}. Session stored at {session.STATE_FILE} (mode 0600)."
        )
    else:
        print(auth.code_sent_message(address))


@app.command()
def logout() -> None:
    """Forget the stored session token and the local change snapshot."""
    # Keep the account address. Dropping it sent the next `benepass login` into
    # "no email — this is the first login on this machine", which is untrue and
    # the opposite of what the tool promised. The snapshot does go: it holds
    # this account's balances, merchant names and transaction ids, and would
    # otherwise be diffed against whichever account logs in next.
    session.clear(keep_account=True)
    cache.clear()
    print(
        "Session cleared (the local change snapshot too; the account address is kept)."
    )


@app.command()
def whoami(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Show the logged-in user and active workspace."""
    data = run(lambda c: c.get("/v2/me/"))
    if as_json:
        emit_json(data)
        return
    user = data.get("data", data) if isinstance(data, dict) else {}
    name = (
        " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "-"
    )
    print(f"Name:      {name}")
    print(f"Email:     {user.get('email', session.email() or '-')}")
    print(f"Workspace: {session.load().get('workspace_id', '-')}")


@app.command()
def workspaces(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """List your Benepass workspaces (employment + personal)."""
    rows = run(lambda c: c.workspaces())
    if as_json:
        emit_json(rows)
        return
    print(
        output.table(
            [
                [
                    w.get("id", "-"),
                    w.get("type", "-"),
                    str(w.get("name") or w.get("organization_name") or "-"),
                ]
                for w in rows
            ],
            ["ID", "TYPE", "NAME"],
        )
    )


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


@app.command(name="benefits")
def benefits_cmd(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Include ids and per-expense caps."
    ),
) -> None:
    """List the benefits you are enrolled in, with available balances."""
    rows = run(list_benefits)
    notify(cache.build(rows, []))
    if as_json:
        emit_json(rows)
        return
    if verbose:
        print(
            output.table(
                [
                    [
                        b["id"],
                        str(b["name"] or "-"),
                        str(b["benefit_type"] or "-"),
                        b["available_usd"] or "-",
                        b["available"],
                        (
                            f"{b['max_per_expense']} {b['max_per_expense_ccy']}"
                            if b["max_per_expense"]
                            else "-"
                        ),
                    ]
                    for b in rows
                ],
                [
                    "BENEFIT ID",
                    "NAME",
                    "TYPE",
                    "AVAIL USD",
                    "AVAIL LOCAL",
                    "MAX/EXPENSE",
                ],
            )
        )
        print(
            "\nMAX/EXPENSE carries its own currency, per row: Benepass states the cap in the"
            f"\naccount's local currency on some enrollments ({output.LOCAL_CCY}) and in USD on"
            "\nothers, so compare a receipt against it only in the currency the row names."
            "\nAVAIL USD is the ledger; AVAIL LOCAL is the same money converted — never compare"
            "\nthe two, and never sum a receipt's own figure against either without converting."
        )
    else:
        print(
            output.table(
                [[str(b["name"] or "-"), b["available"]] for b in rows],
                ["BENEFIT", f"AVAILABLE ({output.LOCAL_CCY})"],
            )
        )


@app.command()
def balances(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Show every benefit account balance (alias of `benefits`, full account detail)."""
    if as_json:
        emit_json(run(lambda c: c.accounts()))
        return
    benefits_cmd(as_json=False, verbose=True)


@app.command()
def expiring(
    days: int = typer.Option(
        150, "--days", help="Look this far ahead for expiration events."
    ),
    all_events: bool = typer.Option(
        False, "--all", help="Include contributions, not just expirations."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Show what you are about to LOSE — balance that expires, and when.

    This is the money question. Benepass caps how much of each balance rolls
    over past its expiration event; anything above that cap is forfeited
    silently, and nothing emails you about it.
    """
    from datetime import date, timedelta

    until = (date.today() + timedelta(days=days)).isoformat()

    def _fetch(
        client: api.Client,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        found = []
        # Balances for the change nudge, taken from the accounts this command
        # already fetches — `benefits` and `transactions` nudge the same way, and
        # paying an extra API call for it here would not be worth it.
        snapshot_benefits = []
        for acct in client.accounts():
            enrollment = acct.get("enrollment") or {}
            benefit = enrollment.get("benefit") or {}
            account_id = acct.get("id")
            if not account_id:
                continue
            balance = output.available_balance(acct)
            if benefit.get("id"):
                snapshot_benefits.append(
                    {
                        "id": benefit["id"],
                        "name": benefit.get("name"),
                        "available_cents": balance.get("amount"),
                    }
                )
            rollover = client.max_rollover(account_id)
            cap = rollover.get("max_rollover_amount")

            # Benepass computes every expiration event's amount from the balance as
            # it stands TODAY — it does not apply the contributions scheduled before
            # that date. So a pot sitting under its rollover cap right now reports
            # $0.00 at risk forever, even when the contributions due before the
            # expiry date will push it over that cap and the excess will die.
            # We walk the schedule ourselves and project, assuming no spending —
            # which is the honest worst case, and the one worth being warned about.
            projected = balance.get("amount")
            events = sorted(
                client.next_events(account_id, until),
                key=lambda e: str(
                    e.get("reference_time") or e.get("execution_time") or ""
                ),
            )
            for event in events:
                kind = event.get("event_type")
                amount = event.get("amount")
                projected_at_risk = None
                if projected is not None:
                    if kind == "contribution" and amount:
                        projected += amount
                    elif kind == "expiration":
                        projected_at_risk = max(0, projected - (cap or 0))
                        projected -= projected_at_risk

                if not all_events and kind != "expiration":
                    continue
                found.append(
                    {
                        "benefit": benefit.get("name"),
                        "benefit_id": benefit.get("id"),
                        "date": str(
                            event.get("reference_time")
                            or event.get("execution_time")
                            or ""
                        )[:10],
                        # All USD cents. The balance's `formatted_local_amount` is
                        # the local-currency conversion at the day's rate, so mixing
                        # the two in one row silently compares different currencies
                        # — keep this column USD.
                        "event_type": kind,
                        "at_risk_usd": (
                            output.usd_abs(amount) if kind == "expiration" else None
                        ),
                        "projected_at_risk_usd": (
                            output.usd_abs(projected_at_risk)
                            if projected_at_risk is not None
                            else None
                        ),
                        "projected_at_risk_cents": projected_at_risk,
                        "amount_usd": output.usd(amount),
                        "available_usd": output.usd(balance.get("amount")),
                        "available_cents": balance.get("amount"),
                        "rolls_over_cents": cap,
                        "available_local": balance.get("formatted_local_amount"),
                        "rolls_over_usd": output.usd(cap),
                    }
                )
        return (
            sorted(found, key=lambda r: (r["date"], r["benefit"] or "")),
            snapshot_benefits,
        )

    rows, snapshot_benefits = run(_fetch)
    notify(cache.build(snapshot_benefits, []))
    if as_json:
        emit_json(rows)
        return
    if not rows:
        print(f"No expiration events scheduled in the next {days} days.")
        return

    def _at_risk(r: dict[str, Any]) -> str:
        """What is forfeited on the date.

        Benepass reports a null event amount on some annual pots. That is not "no
        rollover" — the rollover IS reported, often as $0.00 — so fall back to the
        arithmetic Benepass itself uses: available minus the rollover cap. It
        reconciles exactly against the events that do carry an amount.
        """
        if r.get("at_risk_usd"):
            return r["at_risk_usd"]
        avail, cap = r.get("available_cents"), r.get("rolls_over_cents")
        if avail is None:
            return "?"
        exposure = max(0, avail - (cap or 0))
        return f"~{output.usd_abs(exposure)}"

    table_rows = [
        [
            r["date"],
            str(r["benefit"] or "-")[:34],
            str(r.get("event_type") or "-"),
            r["available_usd"] or "-",
            _at_risk(r) if r.get("event_type") == "expiration" else "-",
            (r.get("projected_at_risk_usd") or "-")
            if r.get("event_type") == "expiration"
            else "-",
            r["rolls_over_usd"] or "-",
            str(r["available_local"] or "-"),
        ]
        for r in rows
    ]
    print(
        output.table(
            table_rows,
            [
                "DATE",
                "BENEFIT",
                "EVENT",
                "AVAIL USD",
                "AT RISK NOW",
                "PROJECTED",
                "ROLLS OVER USD",
                "AVAIL LOCAL",
            ],
        )
    )
    print(
        "\nAT RISK NOW is Benepass's own figure, computed from TODAY's balance — it ignores"
        "\ncontributions scheduled before the date, so a pot under its cap reads 0.00 forever."
        "\nPROJECTED walks the schedule and applies them, assuming no spending: that is the"
        "\nnumber that catches a pot about to be pushed over its cap. ROLLS OVER is the cap."
        "\nA `~` figure is our own estimate (available minus the rollover cap) because Benepass"
        "\nreported no amount for that event — it is exact for the pots that roll over nothing."
        "\nOnly `expiration` rows lose money; `contribution` rows are money arriving."
        "\nBalances are held in USD; AVAIL LOCAL is the same money in the local currency at the"
        "\nday's rate, which is what the Benepass app shows. Don't compare the two columns."
    )


@app.command()
def transactions(
    limit: int = typer.Option(20, "--limit", help="Results per page (max 100)."),
    offset: int = typer.Option(0, "--offset", help="Skip this many results."),
    benefit: str = typer.Option(None, "--benefit", help="Filter to one benefit id."),
    since: str = typer.Option(
        None, "--since", help="On or after this date (YYYY-MM-DD)."
    ),
    until: str = typer.Option(None, "--until", help="Before this date (YYYY-MM-DD)."),
    kind: str = typer.Option(
        None,
        "--type",
        help="card | reimbursement | employer_contribution | expiration (one only).",
    ),
    search: str = typer.Option(None, "--search", help="Merchant name substring."),
    purchase_dates: bool = typer.Option(
        True,
        "--purchase-dates/--no-purchase-dates",
        "-p/-P",
        help="PURCHASED column, on by default (one extra API call per reimbursement row).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """List transactions and submitted expenses.

    Server-side filters. Benepass silently IGNORES a parameter it doesn't
    recognise and still returns 200, so never trust a new filter that doesn't
    change the row count.

    The list endpoint carries no claim data, so the PURCHASED column (the date
    on the receipt, not the filing date) costs one extra fetch per reimbursement
    row. It is on by default because filing-date-only output made two different
    billing months read as duplicates; --no-purchase-dates skips the fetches.
    """

    def _fetch(client: api.Client) -> tuple[Any, dict[str, str]]:
        data = client.transactions(
            limit=limit,
            offset=offset,
            benefit=benefit,
            since=since,
            until=until,
            kind=kind,
            search=search,
        )
        # Transactions carry an account id, not a benefit — resolve names once.
        names = {}
        for acct in client.accounts():
            bname = ((acct.get("enrollment") or {}).get("benefit") or {}).get("name")
            if acct.get("id") and bname:
                names[acct["id"]] = bname
        return data, names

    data, benefit_names = run(_fetch)
    # Only an unfiltered first page is comparable with the cache. `changes`
    # snapshots the newest 100 rows unfiltered, so any row outside that window —
    # everything a --since/--search/--offset query is for — reads as new here and
    # can never be acknowledged: the nudge would repeat on every such query while
    # `changes` itself said "No changes."
    if not any((benefit, since, until, kind, search, offset)):
        notify(cache.build([], api.rows(data)))
    if as_json:
        emit_json(data)
        return
    rows = api.rows(data)

    purchased: dict[str, str] = {}
    if purchase_dates:

        def _detail_dates(client: api.Client) -> dict[str, str]:
            out: dict[str, str] = {}
            for txn in rows:
                if txn.get("transaction_type") != "reimbursement":
                    continue
                detail = client.transaction(str(txn.get("id")))
                # Two shapes in live data — MM/DD/YYYY from this CLI, an ISO
                # timestamp from the web app — so the shared parser reads it.
                when = output.claim_purchase_date(detail)
                if when:
                    out[str(txn.get("id"))] = when
            return out

        purchased = run(_detail_dates)

    table_rows = []
    for txn in rows:
        acct = txn.get("account")
        acct_id = acct.get("id") if isinstance(acct, dict) else acct
        row = [
            str(txn.get("id", "-")),
            str(txn.get("transaction_time") or "-")[:10],
            str(txn.get("merchant_name") or txn.get("title") or "-")[:28],
            output.row_amount(txn),
            # Card/reimbursement rows are shown in the merchant's currency; the
            # system events (contributions, expirations) are recorded in USD and
            # NOT converted, so an unlabelled column mixes the two ~39% apart.
            output.row_ccy(txn),
            str(benefit_names.get(acct_id or "", "-"))[:34],
            str(txn.get("transaction_type") or "-")[:14],
            str(txn.get("transaction_status") or "-"),
        ]
        if purchase_dates:
            # Card swipes purchase at transaction time; reimbursements carry the
            # receipt's date in the claim, which can be months earlier.
            if txn.get("transaction_type") == "card":
                row.append(str(txn.get("transaction_time") or "-")[:10])
            else:
                row.append(purchased.get(str(txn.get("id")), "-"))
        table_rows.append(row)
    headers = ["ID", "DATE", "MERCHANT", "AMOUNT", "CCY", "BENEFIT", "TYPE", "STATUS"]
    if purchase_dates:
        headers.append("PURCHASED")
    print(output.table(table_rows, headers))
    total = data.get("total_count") if isinstance(data, dict) else None
    if total is not None:
        print(f"\n{len(rows)} of {total} (offset {offset})")


@app.command()
def show(
    txn_id: str = typer.Argument(
        ..., help="Expense or transaction id (expense_xxx / txn_xxx)."
    ),
) -> None:
    """Show one expense/transaction in full (always JSON — the record is deeply nested)."""
    emit_json(run(lambda c: c.transaction(txn_id)))


@app.command()
def requirements(
    benefit_id: str = typer.Argument(
        ..., help="Benefit id (benefit_xxx) — see `benepass benefits -v`."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Show what a benefit requires when submitting (note? receipt?)."""
    data = run(lambda c: c.substantiation(benefit_id))
    if as_json:
        emit_json(data)
        return
    rows = api.rows(data)
    if not rows:
        emit_json(data)
        return
    # item_data_type / user_description live under `substantiation_format`, not on
    # the row. Reading them off the row printed "-" in both columns — i.e. the
    # "what qualifies?" half of the command, which is the half the skill tells an
    # agent to consult before filing.
    print(
        output.table(
            [
                [
                    str(r.get("item_type", "-")),
                    str(
                        (r.get("substantiation_format") or {}).get("item_data_type")
                        or "-"
                    ),
                    "yes" if r.get("required") else "no",
                    str(((r.get("rule") or {}).get("specification")) or "-")[:34],
                ]
                for r in rows
            ],
            ["ITEM", "DATA TYPE", "REQUIRED", "WHEN"],
        )
    )
    print()
    for r in rows:
        fmt = r.get("substantiation_format") or {}
        desc = fmt.get("user_description")
        if not desc:
            continue
        print(f"{r.get('item_type')}:")
        for line in str(desc).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line.strip():
                print(f"  {line.rstrip()}")
        print()


@app.command()
def currencies(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """List currency codes and their Benepass ids."""
    rows = run(lambda c: c.currencies())
    if as_json:
        emit_json(rows)
        return
    print(
        output.table(
            [[c.get("id", "-"), str(c.get("code") or "-")] for c in rows],
            ["ID", "CODE"],
        )
    )


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


cli_catalog.register(app)
cli_match.register(app)
cli_profile.register(app)
cli_submit.register(app)


if __name__ == "__main__":
    # The wrapper invokes this as `python -m benepass.cli`; naming it here keeps
    # the help output saying `benepass`.
    app(prog_name="benepass")
