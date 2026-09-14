"""The `api` escape hatch must not be a way around the --confirm gate.

`api /v2/me/expenses/ -X POST` files a claim exactly as `submit --confirm` does,
so it previews by default too — otherwise the one control the docs call reliable
has a hole an over-eager or prompt-injected session can walk through.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from benepass import api, cli_submit
from benepass.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Any call that reaches the client is recorded instead of being sent."""
    sent: list[tuple[str, str]] = []

    class FakeClient:
        def request(self, method: str, path: str, **_: Any) -> Any:
            sent.append((method, path))
            return {"ok": True}

    monkeypatch.setattr(
        cli_submit,
        "run",
        lambda fn, *a, **k: fn(FakeClient()),
    )
    return sent


def test_a_post_previews_instead_of_writing(no_network: list[tuple[str, str]]) -> None:
    result = runner.invoke(
        app, ["api", "/v2/me/expenses/", "-X", "POST", "--body", '{"a": 1}']
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["method"] == "POST"
    assert no_network == []


def test_confirm_sends_the_write(no_network: list[tuple[str, str]]) -> None:
    result = runner.invoke(
        app, ["api", "/v2/me/claims/x/", "-X", "PATCH", "--body", "{}", "--confirm"]
    )
    assert result.exit_code == 0
    assert no_network == [("PATCH", "/v2/me/claims/x/")]


def test_a_get_needs_no_confirmation(no_network: list[tuple[str, str]]) -> None:
    result = runner.invoke(app, ["api", "/v2/me/accounts/"])
    assert result.exit_code == 0
    assert no_network == [("GET", "/v2/me/accounts/")]


def test_the_path_is_validated_before_any_credential_is_attached() -> None:
    """Client.request refuses it, so even --confirm cannot retarget the host."""
    with pytest.raises(api.BenepassError, match="invalid API path"):
        api.build_url("@example.invalid/v2/me/")
