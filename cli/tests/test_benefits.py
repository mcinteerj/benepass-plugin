"""`benefits -v` — the per-expense cap, and the currency nobody states.

Benepass puts that cap in two different fields, one in the account's local
currency and one in USD, and a single account can carry each on different
enrollments. The figure alone is therefore meaningless: a sweep summing
receipts against an unlabelled cap either invents an overrun or clears one,
and nothing in the output would show which.
"""

from __future__ import annotations

from typing import Any

from benepass import output
from benepass.runner import list_benefits


class FakeClient:
    def __init__(self, accounts: list[dict[str, Any]]) -> None:
        self._accounts = accounts

    def accounts(self) -> list[dict[str, Any]]:
        return self._accounts


def _account(account_id: str, **enrollment: Any) -> dict[str, Any]:
    return {
        "id": account_id,
        "balances": [
            {
                "key": f"{account_id}/available",
                "amount": 50000.0,
                "formatted_local_amount": "$500.00",
            }
        ],
        "enrollment": {
            "benefit": {"id": "benefit_x", "name": "Wellness", "benefit_type": "PERK"},
            **enrollment,
        },
    }


def test_a_local_cap_is_labelled_local() -> None:
    rows = list_benefits(
        FakeClient([_account("account_1", local_max_expense_amount=150)])  # type: ignore[arg-type]
    )

    assert rows[0]["max_per_expense"] == 150
    assert rows[0]["max_per_expense_ccy"] == output.LOCAL_CCY


def test_a_cap_stated_only_in_usd_is_labelled_usd() -> None:
    """The fallback field is the USD one — same column, different money."""
    rows = list_benefits(
        FakeClient([_account("account_2", max_expense_amount=200)])  # type: ignore[arg-type]
    )

    assert rows[0]["max_per_expense"] == 200
    assert rows[0]["max_per_expense_ccy"] == "USD"


def test_no_cap_at_all_carries_no_figure_to_mislabel() -> None:
    rows = list_benefits(FakeClient([_account("account_3")]))  # type: ignore[arg-type]

    assert rows[0]["max_per_expense"] is None
