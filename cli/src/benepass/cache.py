"""Local snapshot of Benepass state, so a session can be told what changed.

Why this exists: an agent that runs `benepass benefits` has no way of knowing a
new transaction landed, a balance moved, or the employer edited which categories
a benefit accepts. The cache is the "last acknowledged state" — **only
`benepass changes` writes it**. The commands that already fetch the relevant data
(`benefits`, `transactions`, `expiring`) read it and warn, so the notice keeps
nagging until someone actually looks; the rest stay silent rather than pay for an
extra API call just to check. `transactions` warns only on an **unfiltered first
page**: the snapshot is the newest 100 rows unfiltered, so an older row pulled up
by `--since`/`--search`/`--offset` is absent from it for a reason `changes` can
never resolve, and comparing there would nag for ever about nothing.

Stored next to the session at ~/.config/benepass/cache.json (mode 0600). It
holds balances, transaction ids and category names — no receipts, no tokens.
Nothing leaves the machine.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import output, session

CACHE_FILE = session.STATE_DIR / "cache.json"
# Transactions are immutable once closed and the list is small; keeping the most
# recent 300 ids covers far more than a typical year of perk spending produces,
# while bounding the file if that ever stops being true.
MAX_TRACKED_TXNS = 300


def load() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(snapshot: dict[str, Any]) -> None:
    # Same create-at-0600-then-rename path as the session file: this holds
    # merchant names and balances, and a truncated write would read as "no
    # cache" rather than as an error.
    session.write_private(CACHE_FILE, json.dumps(snapshot, indent=2))


def clear() -> None:
    CACHE_FILE.unlink(missing_ok=True)


def build(
    benefits: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reduce live API data to the comparable subset worth remembering."""
    cats_by_benefit = {}
    for row in catalog_rows or []:
        cats_by_benefit[row["benefit_id"]] = sorted(
            str(c.get("name")) for c in row.get("categories") or [] if c.get("name")
        )

    snap: dict[str, Any] = {
        "captured_at": int(time.time()),
        "benefits": {
            b["id"]: {
                "name": b.get("name"),
                "available_cents": b.get("available_cents"),
                "categories": cats_by_benefit.get(b["id"]),
            }
            for b in benefits
        },
        "transactions": {},
    }
    for txn in transactions[:MAX_TRACKED_TXNS]:
        if txn.get("id"):
            snap["transactions"][txn["id"]] = {
                "date": str(txn.get("transaction_time") or "")[:10],
                "merchant": txn.get("merchant_name") or txn.get("title"),
                # Same pair the transactions table shows — an amount cached
                # without its currency is a number nobody can check.
                "amount": output.row_amount(txn),
                "currency": output.row_ccy(txn),
                "type": txn.get("transaction_type"),
                "status": txn.get("transaction_status"),
            }
    return snap


def _usd(cents: Any) -> str:
    try:
        return f"${float(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return "?"


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable changes from `old` to `new`. Empty list means nothing moved."""
    if not old:
        return []
    changes: list[str] = []

    # A snapshot may be PARTIAL — `benepass benefits` captures no transactions and
    # `benepass transactions` captures no benefits. An absent section means "not
    # looked at", so comparing it would report every item as removed.
    old_b, new_b = old.get("benefits") or {}, new.get("benefits") or {}
    for bid, cur in (new_b if new_b else {}).items():
        name = cur.get("name") or bid
        prev = old_b.get(bid)
        if prev is None:
            changes.append(f"NEW BENEFIT: {name} ({_usd(cur.get('available_cents'))})")
            continue
        if prev.get("available_cents") != cur.get("available_cents"):
            changes.append(
                f"balance: {name} {_usd(prev.get('available_cents'))} → {_usd(cur.get('available_cents'))}"
            )
        # Categories are None when a snapshot skipped the (slower) catalog fetch —
        # absent is "unknown", not "empty", so never report it as a removal.
        before, after = prev.get("categories"), cur.get("categories")
        if before is not None and after is not None and before != after:
            added = [c for c in after if c not in before]
            removed = [c for c in before if c not in after]
            if added:
                changes.append(
                    f"ELIGIBILITY: {name} gained category {', '.join(added)}"
                )
            if removed:
                changes.append(
                    f"ELIGIBILITY: {name} lost category {', '.join(removed)}"
                )
    if new_b:
        for bid, prev in old_b.items():
            if bid not in new_b:
                changes.append(f"BENEFIT REMOVED: {prev.get('name') or bid}")

    old_t, new_t = old.get("transactions") or {}, new.get("transactions") or {}
    for tid, txn in (new_t if new_t else {}).items():
        if tid not in old_t:
            money = " ".join(p for p in (txn.get("amount"), txn.get("currency")) if p)
            changes.append(
                f"new {txn.get('type') or 'transaction'}: {txn.get('date')} "
                f"{txn.get('merchant') or '-'} {money} ({tid})"
            )
        elif old_t[tid].get("status") != txn.get("status"):
            changes.append(
                f"status: {txn.get('merchant') or tid} "
                f"{old_t[tid].get('status')} → {txn.get('status')}"
            )
    return changes


def pending_notice(new: dict[str, Any]) -> str | None:
    """One-line warning for commands that aren't `changes` themselves."""
    count = len(diff(load(), new))
    if not count:
        return None
    return f"benepass: {count} change(s) since you last ran `benepass changes` — run it to see them."
