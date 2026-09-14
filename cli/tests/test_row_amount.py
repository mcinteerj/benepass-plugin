"""The amount/currency pair every benepass command prints for a transaction.

Fixtures are shaped field-for-field like live `benepass transactions --json`
payloads, with invented merchants and ids. The bug they guard against is not a
crash: it is a plausible-looking number under the wrong currency code, which
reads as correct until someone compares it with the receipt.
"""

from __future__ import annotations

from typing import Any

from benepass import output

# A claim FILED in GBP by an employee whose local currency is EUR. The pot is
# debited in USD and Benepass additionally converts to the local currency, so the
# row carries three different numbers — and only formatted_merchant_amount
# matches the receipt's own figure.
FOREIGN_REIMBURSEMENT: dict[str, Any] = {
    "id": "expense_0000000000000000000001",
    "transaction_type": "reimbursement",
    "merchant_name": "Example Bookshop",
    "merchant_currency": {"code": "GBP"},
    "merchant_amount": -4820,
    "formatted_merchant_amount": "-£48.20",
    "amount": -6113,
    "formatted_local_amount": "-€56.40",
}

# Filed in USD, so the ledger amount IS the receipt; the merchant rendering says
# the same thing and is preferred. formatted_local_amount is the local-currency
# conversion, and printing it under the USD label would understate the claim.
USD_REIMBURSEMENT: dict[str, Any] = {
    "id": "expense_0000000000000000000002",
    "transaction_type": "reimbursement",
    "merchant_name": "Example News Subscription",
    "merchant_currency": {"code": "USD"},
    "merchant_amount": -1250,
    "formatted_merchant_amount": "-$12.50",
    "amount": -1250,
    "formatted_local_amount": "-€11.55",
}

# Four orders of magnitude apart: the local conversion under an IDR label is not
# a rounding difference, it is a different number entirely.
IDR_CARD: dict[str, Any] = {
    "id": "ictxn_0000000000000000000003",
    "transaction_type": "card",
    "merchant_name": "Example Warung",
    "merchant_currency": {"code": "IDR"},
    "merchant_amount": -98450000,
    "formatted_merchant_amount": "-Rp984.500,00",
    "amount": -6120,
    "formatted_local_amount": "-€56.48",
}

# A system event has no merchant at all: the USD ledger is the only rendering it
# has, and USD is what row_ccy labels it.
EMPLOYER_CONTRIBUTION: dict[str, Any] = {
    "id": "ictxn_0000000000000000000004",
    "transaction_type": "employer_contribution",
    "merchant_name": None,
    "merchant_currency": None,
    "merchant_amount": None,
    "formatted_merchant_amount": None,
    "amount": 25000,
    "formatted_local_amount": "€230.75",
}


def test_foreign_claim_shows_the_receipt_not_the_local_conversion() -> None:
    assert output.row_amount(FOREIGN_REIMBURSEMENT) == "-£48.20"
    assert output.row_ccy(FOREIGN_REIMBURSEMENT) == "GBP"


def test_usd_claim_shows_the_receipt_not_the_local_conversion() -> None:
    assert output.row_amount(USD_REIMBURSEMENT) == "-$12.50"
    assert output.row_ccy(USD_REIMBURSEMENT) == "USD"


def test_idr_card_row_shows_rupiah() -> None:
    assert output.row_amount(IDR_CARD) == "-Rp984.500,00"
    assert output.row_ccy(IDR_CARD) == "IDR"


def test_employer_contribution_falls_back_to_the_usd_ledger() -> None:
    assert output.row_amount(EMPLOYER_CONTRIBUTION) == "$250.00"
    assert output.row_ccy(EMPLOYER_CONTRIBUTION) == "USD"


def test_every_fixture_amount_is_labelled_by_its_own_currency() -> None:
    """No row may print the employee-local conversion under a foreign label."""
    for row in (FOREIGN_REIMBURSEMENT, USD_REIMBURSEMENT, IDR_CARD):
        assert output.row_amount(row) != row["formatted_local_amount"]
