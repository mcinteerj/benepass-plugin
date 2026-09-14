"""Shared plumbing for the CLI's command modules: auth retry, failure, change notice."""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, NoReturn, TypeVar

import typer

from . import api, auth, cache, output, session

T = TypeVar("T")

# Reserved so a caller can tell "you need to log in" apart from any other
# failure: everything else exits 1.
EXIT_LOGIN_REQUIRED = 3


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"benepass: {message}", file=sys.stderr)
    raise typer.Exit(code)


def run(
    fn: Callable[[api.Client], T], workspace: str | None = None, verbose: bool = False
) -> T:
    """Run an API call, transparently re-logging in once if the session has expired."""
    try:
        client = auth.client(workspace, verbose=verbose)
        try:
            return fn(client)
        except api.AuthExpired:
            if verbose:
                print("Session expired; logging in again...", file=sys.stderr)
            session.clear_token()
            client = auth.client(workspace, verbose=verbose)
            return fn(client)
    except auth.LoginRequired as exc:
        fail(str(exc), EXIT_LOGIN_REQUIRED)
    except api.BenepassError as exc:
        fail(str(exc))


def notify(snapshot: dict[str, Any]) -> None:
    """Warn (on stderr) that state moved since the last `benepass changes`.

    Deliberately does NOT update the cache: only `changes` acknowledges, so the
    notice keeps nagging until someone actually looks at it.
    """
    if os.environ.get("BENEPASS_NO_CHECK"):
        return
    notice = cache.pending_notice(snapshot)
    if notice:
        print(notice, file=sys.stderr)


def list_benefits(client: api.Client) -> list[dict[str, Any]]:
    """Benepass has no benefits-list endpoint; the metadata lives inside each account."""
    result = []
    for acct in client.accounts():
        benefit = (acct.get("enrollment") or {}).get("benefit") or {}
        if not benefit.get("id"):
            continue
        bal = output.available_balance(acct)
        enrollment = acct.get("enrollment") or {}
        # The per-expense cap comes in two currencies and the field names are
        # the only thing that says which: `local_max_expense_amount` is in the
        # account's local currency, `max_expense_amount` in USD. One row can
        # carry each, so the reading has to travel with the figure — an
        # unlabelled cap compared against a receipt is a wrong answer nobody
        # can see is wrong.
        local_cap = enrollment.get("local_max_expense_amount")
        result.append(
            {
                "id": benefit["id"],
                "name": benefit.get("name"),
                "benefit_type": benefit.get("benefit_type"),
                "account_id": acct.get("id"),
                "available": output.money(bal, "amount", "formatted_local_amount"),
                "available_usd": output.usd(bal.get("amount")),
                "available_cents": bal.get("amount"),
                "max_per_expense": local_cap or enrollment.get("max_expense_amount"),
                "max_per_expense_ccy": output.LOCAL_CCY if local_cap else "USD",
            }
        )
    return result
