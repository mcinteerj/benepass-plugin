"""Output helpers: default = compact text, -v = all fields, --json = raw.

Also the small parsers every command shares — a claim's purchase date and a
formatted money string — because both are read out of fields Benepass renders
inconsistently, and a second copy of either would drift.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any


def emit_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def money(row: dict[str, Any], amount_key: str, formatted_key: str) -> str:
    """Prefer Benepass's own formatted string; fall back to cents/100."""
    formatted = row.get(formatted_key)
    if formatted:
        return str(formatted)
    amount = row.get(amount_key)
    if amount is None:
        return "-"
    return f"{amount / 100:,.2f}"


def table(rows: list[list[str]], headers: list[str]) -> str:
    """Minimal left-aligned table — no dependency, stable for piping."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def available_balance(account: dict[str, Any]) -> dict[str, Any]:
    """The bare `*/available` balance — Benepass also carries reimbursement/payout variants."""
    for bal in account.get("balances") or []:
        key = bal.get("key") or ""
        if key.endswith("/available"):
            return bal
    return {}


def usd(cents: Any) -> str | None:
    """Format Benepass's USD cents, KEEPING the sign. None when the API gave no amount."""
    if cents is None:
        return None
    try:
        value = float(cents) / 100
    except (TypeError, ValueError):
        return None
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def usd_abs(cents: Any) -> str | None:
    """Magnitude only — for columns like AT RISK where the sign is already implied."""
    if cents is None:
        return None
    try:
        return f"${abs(float(cents)) / 100:,.2f}"
    except (TypeError, ValueError):
        return None


# Contributions and expirations are recorded in USD with no conversion; card and
# reimbursement rows carry the merchant's own currency. Same column, different
# units — hence a CCY label beside every amount these two produce. The tool
# assumes no particular local currency: it prints whatever the row says it is.
SYSTEM_EVENT_TYPES = {"employer_contribution", "expiration"}
LOCAL_CCY = "LOCAL"


def row_ccy(txn: dict[str, Any]) -> str:
    """Currency of a transaction row's displayed amount (see row_amount)."""
    if txn.get("transaction_type") in SYSTEM_EVENT_TYPES:
        return "USD"
    cur = txn.get("merchant_currency")
    if isinstance(cur, dict):
        return str(cur.get("code") or LOCAL_CCY)
    return str(cur or LOCAL_CCY)


def row_amount(txn: dict[str, Any]) -> str:
    """Amount to display, in the currency row_ccy names.

    Benepass renders one transaction THREE ways, and they are three different
    numbers on any purchase not made in the employee's local currency:

    - ``formatted_merchant_amount`` (labelled by ``merchant_currency``) — what
      the receipt says. The only rendering that agrees with the CCY column, so
      it is the one to show.
    - ``amount`` — USD cents, the ledger figure actually taken off the pot.
      Exact, but in USD whatever the merchant charged.
    - ``formatted_local_amount`` — Benepass's conversion into the employee's own
      local currency. It carries no label of its own, so printing it under a
      merchant currency code asserts something false: a claim filed in the
      merchant's currency would be shown as the converted figure, still wearing
      the merchant's currency code — a real number matching neither the receipt
      nor the pot.

    Hence: merchant rendering first; the USD ledger for system events and for
    rows whose merchant fields are null (employer contributions, expirations),
    where it is both exact and correctly labelled; the local conversion only as
    a last resort.
    """
    if txn.get("transaction_type") not in SYSTEM_EVENT_TYPES:
        merchant = txn.get("formatted_merchant_amount")
        if merchant:
            return str(merchant)
    if row_ccy(txn) == "USD" and txn.get("amount") is not None:
        return usd(txn["amount"]) or "-"
    return str(txn.get("formatted_local_amount") or "-")


# --------------------------------------------------------------------------
# Parsing what Benepass renders
# --------------------------------------------------------------------------


def parse_date(raw: Any) -> str | None:
    """A Benepass date in any of its shapes, as YYYY-MM-DD. None if unreadable.

    Three shapes are in live data and all three have to be read:

    - ``09/13/2026`` — MM/DD/YYYY, what this CLI itself submits;
    - ``2026-09-13`` — plain ISO;
    - ``2025-12-29T13:00:00.000Z`` — an ISO timestamp, what the Benepass web app
      writes.

    **The timestamp form can be a day out.** The app stores the purchase date as
    the user's local midnight converted to UTC, so a 30 December purchase in
    UTC+13 is filed as ``2025-12-29T13:00:00Z`` — and the date is not carrying a
    timezone that would let us undo that. Taking the UTC date is the honest
    reading; anything that compares these dates has to tolerate a day either
    way, which is why `match --window` defaults to 3 rather than 0.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def claim_purchase_date(txn: dict[str, Any]) -> str | None:
    """The date on the RECEIPT of a filed claim, as YYYY-MM-DD.

    Not the same thing as ``transaction_time``, which is when the claim was
    FILED — the two are months apart on a backfilled receipt, and it is the
    purchase date that decides whether two rows are the same purchase.
    """
    for item in (txn.get("claim") or {}).get("substantiation_items") or []:
        if item.get("item_type") == "purchase_date":
            return parse_date((item.get("item_detail") or {}).get("value"))
    return None


# ISO 4217 gives these five three minor digits, and every other live currency
# two or none. A fixed list rather than a lookup because both callers need it
# where no currency payload is in hand — reading a rendered figure, and scaling
# a transaction row — and the set has not changed in decades.
THREE_MINOR_DIGIT_CCYS = frozenset({"KWD", "BHD", "OMR", "JOD", "TND"})

# What Benepass publishes per currency when it publishes anything; two is both
# the overwhelming case and the only scale seen live, so it is the fallback.
DEFAULT_MINOR_DIGITS = 2


def minor_digits(row: dict[str, Any]) -> int | None:
    """A currency row's published number of minor digits, when it states one."""
    try:
        return int(row["decimals"])
    except (KeyError, TypeError, ValueError):
        return None


_NUMERIC = re.compile(r"[^0-9.,]")


def money_readings(text: Any) -> list[float]:
    """Every reading of a formatted money string, likeliest first.

    Benepass formats each currency in its own locale — ``$861.35``,
    ``Rp1.357.560,00``, ``1 234,56 kr`` — and never states which separator it
    used, so the shape has to be guessed from the digits. The last separator
    followed by anything other than exactly three digits is a decimal point;
    a lone separator with exactly three digits after it is normally a
    thousands mark.

    **Normally.** The five currencies with three minor digits — KWD, BHD, OMR,
    JOD, TND — render e.g. ``KD153.750``, where that separator IS the decimal
    point and the thousands reading is 1000× too large. Nothing in the string
    says which, so both readings come back (thousands first) and a caller with
    a second source of truth — a published rate, a currency's own ``decimals``
    — decides. Callers with no such source take the first and are right
    everywhere else.

    The sign is dropped: every caller wants a magnitude. An empty list means
    there was no number at all.
    """
    if text is None:
        return []
    cleaned = _NUMERIC.sub("", str(text))
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return []
    dot, comma = cleaned.rfind("."), cleaned.rfind(",")
    last = max(dot, comma)
    if last == -1:
        return [float(cleaned)]

    def _as_decimal_point() -> float:
        whole = cleaned[:last].replace(".", "").replace(",", "")
        return float(f"{whole or 0}.{cleaned[last + 1 :]}")

    decimals = len(cleaned) - last - 1
    if (dot != -1 and comma != -1) or decimals != 3:
        return [_as_decimal_point()]
    return [float(cleaned.replace(".", "").replace(",", "")), _as_decimal_point()]


def parse_money(text: Any) -> float | None:
    """The likeliest reading of a formatted money string. None if there is none.

    The single-answer form of :func:`money_readings` — use that one where the
    caller can check a reading against something else, because a three-minor-
    digit currency is genuinely ambiguous and this function has to pick.
    """
    readings = money_readings(text)
    return readings[0] if readings else None
