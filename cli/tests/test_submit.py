"""What `submit` actually sends, which the preview cannot show on its own.

`merchant_amount` goes to Benepass in the currency's own minor units, and the
preview echoes back the major-unit figure it was handed — so a wrong scale is
invisible at the one point a human checks. These tests pin the scale to what
the API publishes per currency rather than to a constant.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from benepass import api, cli_submit
from benepass.cli import app
from benepass.runner import fail

runner = CliRunner()

CURRENCIES: list[dict[str, Any]] = [
    {"id": "crcy_usd", "code": "USD", "decimals": 2},
    # Recorded in major units, the JPY/KRW shape.
    {"id": "crcy_xzd", "code": "XZD", "decimals": 0},
    # Three minor digits, the KWD/BHD/OMR/JOD/TND shape.
    {"id": "crcy_kwd", "code": "KWD", "decimals": 3},
    # Publishes no scale at all: two is the fallback.
    {"id": "crcy_xnd", "code": "XND"},
]


class FakeClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def currencies(self) -> list[dict[str, Any]]:
        return CURRENCIES

    def substantiation(self, benefit: str) -> list[dict[str, Any]]:
        return []

    def create_expense(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        return {"id": "expense_0000000000000000000001", "claim_status": "pending"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """A client in place of the API, with `run`'s own error translation kept.

    `runner.run` turns a BenepassError into exit 1 with the message on stderr,
    and a test that skipped that would assert against a traceback instead of
    the behaviour a user sees.
    """
    fake = FakeClient()

    def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(fake)
        except api.BenepassError as exc:
            fail(str(exc))

    monkeypatch.setattr(cli_submit, "run", _run)
    return fake


def _preview(currency: str, amount: str) -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "submit",
            "--benefit",
            "benefit_x",
            "--merchant",
            "Example Store",
            "--amount",
            amount,
            "--currency",
            currency,
            "--date",
            "2026-08-27",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


@pytest.mark.parametrize(
    "currency, amount, minor",
    [
        ("USD", "42.50", -4250),
        ("XZD", "12000", -12000),
        ("KWD", "153.750", -153750),
        ("XND", "42.50", -4250),
    ],
)
def test_the_amount_is_scaled_by_the_published_minor_digits(
    client: FakeClient, currency: str, amount: str, minor: int
) -> None:
    assert _preview(currency, amount)["merchant_amount"] == minor


def test_the_preview_says_the_minor_unit_figure_where_the_scale_is_unusual(
    client: FakeClient,
) -> None:
    """Only a 2-decimal currency has been checked against live data.

    The amount line reads the same whatever the scale, so the figure that will
    actually be sent is what the person approving the claim needs to see.
    """
    warnings = _preview("XZD", "12000")["warnings"]

    assert any("12000 in minor units" in w for w in warnings)
    assert not _preview("USD", "42.50")["warnings"]


def test_confirm_sends_the_same_figure_the_preview_showed(client: FakeClient) -> None:
    preview = _preview("XZD", "12000")
    result = runner.invoke(
        app,
        [
            "submit",
            "--benefit",
            "benefit_x",
            "--merchant",
            "Example Store",
            "--amount",
            "12000",
            "--currency",
            "XZD",
            "--date",
            "2026-08-27",
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    assert client.created[0]["merchant_amount"] == preview["merchant_amount"]
    assert client.created[0]["merchant_currency"] == "crcy_xzd"


def test_an_unknown_currency_is_refused_rather_than_guessed(
    client: FakeClient,
) -> None:
    result = runner.invoke(
        app,
        [
            "submit",
            "--benefit",
            "benefit_x",
            "--merchant",
            "Example Store",
            "--amount",
            "10",
            "--currency",
            "ZZZ",
            "--date",
            "2026-08-27",
        ],
    )

    assert result.exit_code == 1
    assert "unknown currency" in result.stderr
    assert client.created == []
