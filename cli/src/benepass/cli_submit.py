"""Write commands (expense submission) plus the raw-API escape hatch.

Split from cli.py to keep each command module small. Registered onto the shared
typer app by `register(app)`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from . import api, output
from .output import emit_json
from .runner import fail, list_benefits, run

MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "heic": "image/heic",
    "webp": "image/webp",
}
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


def _presigned_url(uploaded: Any, label: str) -> str:
    """Pull the receipt URL out of an upload response, whichever envelope it uses."""
    payload = uploaded or {}
    url = payload.get("presigned_url") or (payload.get("data") or {}).get(
        "presigned_url"
    )
    if not url:
        raise api.BenepassError(f"receipt upload for {label} returned no presigned_url")
    return str(url)


def register(app: typer.Typer) -> None:
    def _us_date(value: str) -> str:
        """Parse a purchase date to Benepass's MM/DD/YYYY.

        ISO is the safe input. A slash date is accepted only when it CANNOT be
        read two ways: outside the US, 03/09/2026 means 3 September, while
        Benepass reads it as 9 March — and the preview would echo the ambiguous
        string back unchanged, so the one human gate cannot catch it. Claims can
        finalise in seconds and cannot be amended, so a silent misparse is
        unrecoverable. Refuse instead of guessing.
        """
        from datetime import datetime

        text = value.strip()
        try:
            return datetime.strptime(text, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            pass

        parts = text.replace("-", "/").split("/")
        if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
            a, b = int(parts[0]), int(parts[1])
            if a <= 12 and b <= 12 and a != b:
                raise ValueError(
                    f"{value!r} is ambiguous — it could be {a:02d}/{b:02d} "
                    f"(month/day) or {b:02d}/{a:02d} (day/month). Use YYYY-MM-DD."
                )
            try:
                return datetime.strptime(text, "%m/%d/%Y").strftime("%m/%d/%Y")
            except ValueError:
                # Unambiguous the other way round (day > 12): read as DD/MM/YYYY.
                return datetime.strptime(text, "%d/%m/%Y").strftime("%m/%d/%Y")
        raise ValueError(value)

    def _pretty_date(us: str) -> str:
        """Render MM/DD/YYYY unmistakably, so the preview can actually be checked."""
        from datetime import datetime

        return datetime.strptime(us, "%m/%d/%Y").strftime("%d %B %Y")

    def _check_requirements(
        client: api.Client,
        benefit: str,
        purchase_date: str,
        note: str | None,
        receipts: int,
    ) -> list[str]:
        """Warnings from the benefit's own substantiation policy. Never a hard gate.

        `required` is conditional for some items — a benefit may demand a receipt
        only above a threshold amount — so blocking on the flag would refuse
        legitimate small claims.
        """
        from datetime import datetime

        warnings = []
        try:
            rows = api.rows(client.substantiation(benefit))
        except api.BenepassError:
            return ["could not read this benefit's substantiation policy"]
        for row in rows:
            fmt = row.get("substantiation_format") or {}
            item, required = row.get("item_type"), row.get("required")
            if required and item == "note" and not note:
                warnings.append("this benefit requires a --note")
            if required and item == "receipt" and not receipts:
                warnings.append("this benefit requires a --receipt")
            if item == "purchase_date":
                for val in fmt.get("validations") or []:
                    # Shape: {"key": "date_gte", "rule": {"metadata":
                    # {"frontend_validation_value": "2025-01-01T00:00:00Z"}}}
                    kind = val.get("key")
                    bound = ((val.get("rule") or {}).get("metadata") or {}).get(
                        "frontend_validation_value"
                    )
                    if not bound:
                        continue
                    try:
                        got = datetime.strptime(purchase_date, "%m/%d/%Y").date()
                        lim = datetime.strptime(str(bound)[:10], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if kind == "date_gte" and got < lim:
                        warnings.append(
                            f"purchase date is before the eligible window ({bound})"
                        )
                    if kind == "date_lte" and got > lim:
                        warnings.append(
                            f"purchase date is after the eligible window ({bound})"
                        )
        return warnings

    def _resolve_currency(client: api.Client, currency: str) -> tuple[str, int]:
        """The `crcy_` id for a currency, and the minor digits Benepass gives it.

        The scale is read, not assumed. `merchant_amount` is sent in minor
        units, so a currency Benepass records in major units (JPY, KRW) or with
        three minor digits (KWD and its four peers) would be filed 100x or 10x
        out from a flat x100 — behind a preview that echoes back the figure it
        was handed, which is the one error the human gate cannot catch. The
        currency list is already being fetched to resolve the id and it
        publishes `decimals` per currency, so that is the answer rather than a
        guess. Where it states none, two stands: it is the only scale observed
        live and the overwhelming case.
        """
        wanted = currency.strip().upper()
        for row in client.currencies():
            if (
                str(row.get("id", "")) == currency
                or str(row.get("code", "")).upper() == wanted
            ):
                digits = output.minor_digits(row)
                return str(row["id"]), (
                    output.DEFAULT_MINOR_DIGITS if digits is None else digits
                )
        if currency.startswith("crcy_"):
            # An id the list does not carry: it may still be valid, so it is
            # passed through as before — but nothing states its scale.
            return currency, output.DEFAULT_MINOR_DIGITS
        raise api.BenepassError(
            f'unknown currency "{currency}" — see `benepass currencies`'
        )

    @app.command()
    def submit(
        benefit: str = typer.Option(
            ..., "--benefit", help="Benefit id (benefit_xxx) to claim against."
        ),
        merchant: str = typer.Option(
            ..., "--merchant", help='Merchant name, e.g. "Amazon".'
        ),
        amount: float = typer.Option(
            ...,
            "--amount",
            help="Positive decimal in the merchant currency, e.g. 25.99.",
        ),
        currency: str = typer.Option(
            "USD",
            "--currency",
            help="ISO code of the currency ON THE RECEIPT (USD, GBP, JPY...) "
            "or a crcy_xxx id. Defaults to USD — set it for any other receipt.",
        ),
        date: str = typer.Option(
            ...,
            "--date",
            help="Purchase date, YYYY-MM-DD (or MM/DD/YYYY). Required by every benefit.",
        ),
        note: str = typer.Option(
            None, "--note", help="Short description of the purchase."
        ),
        receipt: list[Path] = typer.Option(
            None, "--receipt", help="Receipt file(s). Repeatable."
        ),
        confirm: bool = typer.Option(
            False, "--confirm", help="Actually submit. Without it, previews only."
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
    ) -> None:
        """Submit an out-of-pocket expense for reimbursement.

        Previews by default — real submission requires --confirm.
        """
        if amount <= 0 or amount > 100_000:
            fail("--amount must be a positive number under 100,000")
        try:
            purchase_date = _us_date(date)
        except ValueError as exc:
            detail = str(exc)
            fail(
                detail
                if "ambiguous" in detail
                else f"--date must be YYYY-MM-DD (preferred) or MM/DD/YYYY, got {date!r}"
            )
        for path in receipt or []:
            if not path.is_file():
                fail(f"receipt not found: {path}")
            if path.stat().st_size > MAX_RECEIPT_BYTES:
                fail(f"receipt {path.name} exceeds Benepass's 10 MB limit")

        def _do(client: api.Client) -> Any:
            currency_id, digits = _resolve_currency(client, currency)
            minor_amount = -round(amount * 10**digits)

            warnings = _check_requirements(
                client, benefit, purchase_date, note, len(receipt or [])
            )
            if digits != output.DEFAULT_MINOR_DIGITS:
                # Every live row this CLI has been checked against was a
                # two-minor-digit currency, so this path is read off the API
                # rather than verified. Say the figure that will actually be
                # sent, in minor units, so the person approving the claim can
                # check the one thing the amount line cannot show them.
                warnings.append(
                    f"{currency.upper()} is published with {digits} decimal place(s), "
                    f"so this claim is sent as {abs(minor_amount)} in minor units — "
                    "check that against the receipt; only 2-decimal currencies have "
                    "been verified against live data"
                )
            if not confirm:
                return {
                    "dry_run": True,
                    "benefit": benefit,
                    "merchant_name": merchant,
                    "amount": amount,
                    "currency": currency,
                    "currency_id": currency_id,
                    "minor_digits": digits,
                    "merchant_amount": minor_amount,
                    "purchase_date": purchase_date,
                    "purchase_date_readable": _pretty_date(purchase_date),
                    "warnings": warnings,
                    "note": note,
                    "receipts": [str(p) for p in receipt or []],
                    "message": "Not submitted. Re-run with --confirm to submit for real.",
                }

            for w in warnings:
                print(f"benepass: WARNING — {w}", file=sys.stderr)

            receipt_urls = []
            for path in receipt or []:
                mime = MIME_BY_EXT.get(
                    path.suffix.lstrip(".").lower(), "application/octet-stream"
                )
                uploaded = client.upload_receipt(path.name, path.read_bytes(), mime)
                receipt_urls.append(_presigned_url(uploaded, path.name))

            items: list[dict[str, Any]] = [
                {"item_type": "purchase_date", "item_detail": {"value": purchase_date}},
            ]
            if note:
                items.append({"item_type": "note", "item_detail": {"value": note}})
            if receipt_urls:
                items.append(
                    {"item_type": "receipt", "item_detail": {"value": receipt_urls}}
                )

            return client.create_expense(
                {
                    "benefit": benefit,
                    "merchant_name": merchant,
                    "merchant_currency": currency_id,
                    # Benepass records out-of-pocket spend as a negative
                    # figure in the currency's own minor units
                    "merchant_amount": minor_amount,
                    "claim": {"substantiation_items": items},
                }
            )

        result = run(_do)
        if as_json or confirm:
            emit_json(result)
            return
        print(f"DRY RUN — would submit {currency} {amount:,.2f} at {merchant}")
        print(f"  benefit:  {benefit}")
        print(f"  date:     {_pretty_date(purchase_date)}  (sent as {purchase_date})")
        print(f"  note:     {note or '(none)'}")
        print(f"  receipts: {', '.join(str(p) for p in receipt or []) or '(none)'}")
        for w in result.get("warnings") or []:
            print(f"  WARNING:  {w}")
        print("\nRe-run with --confirm to submit.")

    @app.command()
    def reclassify(
        transaction_id: str = typer.Argument(
            ..., help="Card transaction id (ictxn_xxx / txn_xxx)."
        ),
        benefit: str = typer.Option(
            ..., "--benefit", help="Benefit id to move the charge to."
        ),
        confirm: bool = typer.Option(
            False, "--confirm", help="Actually move it. Without it, previews."
        ),
    ) -> None:
        """Move a CARD charge to a different benefit — retroactive category correction.

        Benepass auto-classifies each card swipe to a best-fit benefit, and its
        best fit is often the broadest one. Moving a charge onto a narrower
        benefit that also covers it frees the broad pot that has to absorb
        miscellaneous spend later — the priority rule, applied after the fact.

        Benepass rejects a benefit that isn't eligible for that transaction, so a
        wrong guess is refused rather than silently mis-filed.
        """

        def _do(client: api.Client) -> Any:
            match = next((b for b in list_benefits(client) if b["id"] == benefit), None)
            if not match:
                raise api.BenepassError(
                    f"no benefit with id {benefit} — see `benepass benefits -v`"
                )
            account_id = match["account_id"]
            txn = client.transaction(transaction_id)
            if txn.get("transaction_type") != "card":
                raise api.BenepassError(
                    f"{transaction_id} is a {txn.get('transaction_type')}, not a card charge — "
                    "only card transactions can be reclassified."
                )
            if not confirm:
                return {
                    "dry_run": True,
                    "transaction": transaction_id,
                    "merchant": txn.get("merchant_name"),
                    "amount": output.row_amount(txn),
                    "currency": output.row_ccy(txn),
                    "from_account": (txn.get("account") or {}).get("id"),
                    "to_benefit": benefit,
                    "to_account": account_id,
                    "message": "Not moved. Re-run with --confirm.",
                }
            return client.request(
                "PATCH",
                f"/v2/me/transactions/{transaction_id}/",
                json_body={"account": account_id},
            )

        emit_json(run(_do))

    @app.command()
    def delete(
        expense_id: str = typer.Argument(
            ..., help="Reimbursement expense id (expense_xxx)."
        ),
        confirm: bool = typer.Option(
            False, "--confirm", help="Actually delete it. Without it, previews."
        ),
    ) -> None:
        """Withdraw a PENDING reimbursement claim — the mis-file undo.

        Only works while the claim is still pending (`can_update: true`); an
        approved claim is finalized and has no undo. The DELETE path needs its
        trailing slash — without it Benepass no-ops and returns null, which is
        indistinguishable from success — so this command verifies the claim is
        actually gone afterwards and fails loudly if it is not.
        """

        def _do(client: api.Client) -> Any:
            txn = client.transaction(expense_id)
            if txn.get("transaction_type") != "reimbursement":
                raise api.BenepassError(
                    f"{expense_id} is a {txn.get('transaction_type')}, not a reimbursement — "
                    "only filed claims can be deleted (card charges are real spend)."
                )
            claim = txn.get("claim") or {}
            if claim.get("finalized"):
                raise api.BenepassError(
                    f"{expense_id} is finalized (status {claim.get('claim_status')}) — "
                    "no undo exists; an over-claim needs offsetting instead."
                )
            if not confirm:
                return {
                    "dry_run": True,
                    "expense": expense_id,
                    "merchant": txn.get("merchant_name"),
                    # The amount is how a human confirms this is the claim they
                    # meant to withdraw, so it has to be the receipt's figure and
                    # its currency, not the unlabelled local conversion.
                    "amount": output.row_amount(txn),
                    "currency": output.row_ccy(txn),
                    "status": claim.get("claim_status"),
                    "message": "Not deleted. Re-run with --confirm.",
                }
            client.request("DELETE", f"/v2/me/expenses/{expense_id}/")
            try:
                client.transaction(expense_id)
            except api.BenepassError:
                return {"deleted": True, "expense": expense_id}
            raise api.BenepassError(
                f"DELETE returned but {expense_id} still exists — not deleted; "
                "check it in the app."
            )

        emit_json(run(_do))

    @app.command()
    def tasks() -> None:
        """Outstanding Benepass tasks — the 'action required' / 'needs information' queue."""
        data = run(lambda c: c.get("/v2/me/tasks/"))
        rows = (data or {}).get("tasks") or []
        if not rows:
            print("No outstanding tasks.")
            return
        emit_json(rows)

    @app.command()
    def upload(
        receipt: Path = typer.Argument(..., help="Receipt file to upload."),
    ) -> None:
        """Upload a receipt and print its URL, without filing anything.

        `submit` uploads its own receipts. This is the standalone half, for
        repairing a bounced claim: upload the corrected receipt, then PATCH the
        claim's substantiation items with the URL this prints. Uploading files
        nothing and costs nothing if unused.
        """
        if not receipt.is_file():
            fail(f"receipt not found: {receipt}")
        if receipt.stat().st_size > MAX_RECEIPT_BYTES:
            fail(f"receipt {receipt.name} exceeds Benepass's 10 MB limit")
        mime = MIME_BY_EXT.get(
            receipt.suffix.lstrip(".").lower(), "application/octet-stream"
        )

        def _do(client: api.Client) -> Any:
            uploaded = client.upload_receipt(receipt.name, receipt.read_bytes(), mime)
            return {
                "file": str(receipt),
                "presigned_url": _presigned_url(uploaded, receipt.name),
            }

        emit_json(run(_do))

    @app.command(name="api")
    def api_cmd(
        path: str = typer.Argument(..., help="API path, e.g. /v2/me/accounts/"),
        method: str = typer.Option("GET", "-X", "--method", help="HTTP method."),
        body: str = typer.Option(None, "--body", help="JSON request body."),
        confirm: bool = typer.Option(
            False,
            "--confirm",
            help="Required for any method other than GET/HEAD. Without it, previews.",
        ),
    ) -> None:
        """Escape hatch: call any Benepass API path with the stored session.

        A non-GET call through here changes real data — it can file a claim just
        as `submit` does — so it previews by default and needs --confirm, on the
        user's approval of that specific call.
        """
        import json as _json

        try:
            parsed = _json.loads(body) if body else None
        except _json.JSONDecodeError as exc:
            # Hand-typed on this command more than anywhere else, and a raw
            # traceback is not a useful thing to hand a person.
            fail(f"--body is not valid JSON: {exc}")
        verb = method.upper()
        if verb not in ("GET", "HEAD") and not confirm:
            emit_json(
                {
                    "dry_run": True,
                    "method": verb,
                    "path": path,
                    "body": parsed,
                    "message": (
                        f"Not sent. {verb} through `api` writes to the real account — "
                        "re-run with --confirm once the user has approved this call."
                    ),
                }
            )
            return
        emit_json(run(lambda c: c.request(verb, path, json_body=parsed)))
