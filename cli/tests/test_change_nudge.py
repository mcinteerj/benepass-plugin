"""The "N change(s) pending" nudge, and the one query shape it can be true for.

`benepass changes` is the only command that writes the cache, and it snapshots
the newest 100 transactions *unfiltered*. So a row it has never seen — anything
a `--since`, `--search` or `--offset` query exists to surface — would read as
new against that snapshot, and `changes` could never acknowledge it: the nudge
would repeat on every such query while `changes` itself printed "No changes."

Rows are invented, shaped like live `transactions --json` payloads.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from benepass import cache, cli
from benepass.cli import app

runner = CliRunner()


def _row(txn_id: str, date: str) -> dict[str, Any]:
    return {
        "id": txn_id,
        "transaction_type": "card",
        "transaction_status": "complete",
        "transaction_time": f"{date}T09:00:00Z",
        "merchant_name": "Northgate Books",
        "merchant_currency": {"code": "XND"},
        "merchant_amount": -1899,
        "formatted_merchant_amount": "-$18.99",
        "amount": -1899,
        "formatted_local_amount": "-$18.99",
        "account": {"id": "account_1"},
    }


CACHED = _row("ictxn_cached", "2026-09-01")
# Older than anything in the snapshot, so the cache has never held it.
OLDER = _row("ictxn_older", "2024-03-02")


class FakeClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def transactions(self, **kwargs: Any) -> dict[str, Any]:
        return {"data": self.rows}

    def accounts(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture(autouse=True)
def _baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache holding exactly what an unfiltered `changes` would have seen."""
    monkeypatch.delenv("BENEPASS_NO_CHECK", raising=False)
    cache.save(cache.build([], [CACHED]))


def _invoke(rows: list[dict[str, Any]], *args: str) -> Any:
    fake = FakeClient(rows)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "run", lambda fn, *a, **k: fn(fake))
        return runner.invoke(
            app, ["transactions", "--no-purchase-dates", *args], catch_exceptions=False
        )


def test_a_filtered_query_does_not_nag_about_rows_changes_will_never_show() -> None:
    result = _invoke([OLDER], "--since", "2024-01-01")

    assert result.exit_code == 0
    assert "change(s) since" not in result.stderr


def test_a_paged_query_does_not_nag_either() -> None:
    result = _invoke([OLDER], "--offset", "100")

    assert result.exit_code == 0
    assert "change(s) since" not in result.stderr


def test_an_unfiltered_page_still_nags() -> None:
    """The one view that IS comparable with the snapshot keeps its warning."""
    result = _invoke([CACHED, _row("ictxn_new", "2026-09-12")])

    assert result.exit_code == 0
    assert "change(s) since" in result.stderr
