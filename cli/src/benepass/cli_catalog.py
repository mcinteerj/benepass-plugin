"""Eligibility + change-detection commands, split out to keep cli.py small.

Registered onto the shared typer app by `register(app)` at import time in cli.py.
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from . import api, cache, catalog, output
from .output import emit_json
from .runner import list_benefits, run


def register(app: typer.Typer) -> None:
    @app.command()
    def categories(
        benefit: str = typer.Option(None, "--benefit", help="Only this benefit id."),
        examples: bool = typer.Option(
            False, "-e", "--examples", help="Include the example purchases."
        ),
        markdown: bool = typer.Option(
            False,
            "--markdown",
            help="Emit the generated policy reference (see the README).",
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """What each benefit actually accepts — eligible categories and example purchases.

        Benefits are listed narrowest first, which is the order to try them in: the
        fewer categories a benefit accepts, the less else can use that money.
        """

        def _fetch(client: api.Client) -> list[dict[str, Any]]:
            rows = list_benefits(client)
            if benefit:
                rows = [b for b in rows if b["id"] == benefit]
                if not rows:
                    raise api.BenepassError(
                        f"no benefit with id {benefit} — see `benepass benefits -v`"
                    )
            return catalog.catalog(client, rows)

        rows = run(_fetch)

        if as_json:
            emit_json(rows)
            return
        if markdown:
            # sys.stdout.write, not print: to_markdown already ends in exactly
            # one newline, and print's extra one would make every regeneration
            # differ from the last, which reads as a phantom policy change.
            sys.stdout.write(
                catalog.to_markdown(
                    rows,
                    "Read from the Benepass policy endpoints — the app's Policy overview.",
                )
            )
            return

        for row in rows:
            print(
                f"\n{row['benefit']}  ({row['category_count']} categories)  {row['benefit_id']}"
            )
            if row.get("allowed_merchants"):
                print(
                    f"    any purchase allowed at: {', '.join(row['allowed_merchants'])}"
                )
            if row.get("disallowed_merchants"):
                print(
                    f"    nothing allowed at: {', '.join(row['disallowed_merchants'])}"
                )
            for cat in row["categories"]:
                print(f"  - {cat['name']}")
                if examples:
                    if cat["specification"]:
                        print(f"      {cat['specification'][:160]}")
                    for line in cat["examples"].split("\n"):
                        line = line.strip().lstrip("- ").strip()
                        if line:
                            print(f"        · {line}")

    @app.command()
    def merchants(
        benefit: str = typer.Option(
            None, "--benefit", help="Only merchants this benefit accepts."
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """Merchants Benepass recognises, and the categories each one satisfies."""
        rows = run(lambda c: catalog.merchants(c, benefit))
        if as_json:
            emit_json(rows)
            return
        print(
            output.table(
                [
                    [
                        str(m.get("display_name") or m.get("name") or "-")[:28],
                        ", ".join(
                            str(c.get("name"))
                            for c in (m.get("eligibility_categories") or [])
                        )[:60],
                        str(m.get("merchant_url") or "-")[:38],
                    ]
                    for m in rows
                ],
                ["MERCHANT", "CATEGORIES", "URL"],
            )
        )
        print(f"\n{len(rows)} merchant(s){' for ' + benefit if benefit else ''}")

    @app.command()
    def options(
        transaction_id: str = typer.Argument(
            ..., help="Transaction id (ictxn_xxx / expense_xxx)."
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """Which other benefits could legitimately pay for this transaction.

        Benepass answers this itself, with balances and the reason each benefit
        qualifies. Reading it beats guessing a target for `reclassify` and letting
        a PATCH be refused — a write is never the right way to discover a
        read-only fact.
        """

        def _fetch(client: api.Client) -> dict[str, Any]:
            txn = client.transaction(transaction_id)
            names = {}
            for b in list_benefits(client):
                names[b["id"]] = b["name"]
            opts = []
            for bid, info in (txn.get("eligible_benefits") or {}).items():
                opts.append(
                    {
                        "benefit_id": bid,
                        "benefit": names.get(bid),
                        "reason": info.get("eligible"),
                        "balance_usd": output.usd(info.get("balance")),
                        "account_id": (info.get("account") or {}).get("id"),
                    }
                )
            return {
                "transaction": transaction_id,
                "merchant": txn.get("merchant_name") or txn.get("title"),
                "amount": output.row_amount(txn),
                "currency": output.row_ccy(txn),
                "type": txn.get("transaction_type"),
                "current_account": (txn.get("account") or {}).get("id"),
                "options": sorted(opts, key=lambda o: str(o["benefit"] or "")),
            }

        data = run(_fetch)
        if as_json:
            emit_json(data)
            return
        print(
            f"{data['merchant']}  {data['amount']} {data['currency']}  ({data['type']})"
        )
        if not data["options"]:
            print("\nBenepass lists no alternative benefits for this transaction.")
            return
        print()
        print(
            output.table(
                [
                    [
                        str(o["benefit"] or o["benefit_id"])[:34],
                        str(o["balance_usd"] or "-"),
                        str(o["reason"] or "-"),
                        "current" if o["account_id"] == data["current_account"] else "",
                    ]
                    for o in data["options"]
                ],
                ["BENEFIT", "BALANCE USD", "WHY ELIGIBLE", ""],
            )
        )

    @app.command()
    def changes(
        full: bool = typer.Option(
            False,
            "--full",
            help="Also re-read the eligibility catalog (slower; catches category edits).",
        ),
        reset: bool = typer.Option(
            False, "--reset", help="Just re-baseline; report nothing."
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """What moved since you last ran this — new transactions, balances, eligibility.

        This is the only command that writes the cache, so a change stays reported
        until you actually run it.
        """

        def _fetch(client: api.Client) -> dict[str, Any]:
            benefits = list_benefits(client)
            txns = api.rows(client.transactions(limit=100))
            cat_rows = catalog.catalog(client, benefits) if full else None
            return cache.build(benefits, txns, cat_rows)

        snapshot = run(_fetch)
        previous = cache.load()
        found = cache.diff(previous, snapshot)

        # A partial snapshot must not clobber known categories with None, or the next
        # --full run reports every category as newly gained.
        if not full:
            for bid, entry in snapshot.get("benefits", {}).items():
                if entry.get("categories") is None:
                    entry["categories"] = (
                        previous.get("benefits", {}).get(bid) or {}
                    ).get("categories")
        # `api.rows` returns [] for any payload it doesn't recognise, so an envelope
        # change or an HTML 200 is indistinguishable from "you have no accounts".
        # Saving that would wipe the baseline and report everything as new next run.
        for section in ("benefits", "transactions"):
            if not snapshot.get(section) and previous.get(section):
                print(
                    f"benepass: refusing to overwrite the cached {section} — the API "
                    "returned none, which is a transport or schema problem rather "
                    "than a real change.",
                    file=sys.stderr,
                )
                snapshot[section] = previous[section]
        cache.save(snapshot)

        if as_json:
            emit_json({"baseline": not previous, "reset": reset, "changes": found})
            return
        if reset:
            print("Baseline reset.")
            return
        if not previous:
            print(
                f"Baseline captured ({len(snapshot.get('transactions', {}))} transactions, "
                f"{len(snapshot.get('benefits', {}))} benefits). Changes are reported from now on."
            )
            return
        if not found:
            print("No changes.")
            return
        for line in found:
            print(f"  {line}")
        print(f"\n{len(found)} change(s).")
