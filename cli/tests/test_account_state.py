"""Which account the CLI is acting as, and what a logout actually forgets.

Two failures these cover are silent rather than loud: a stored token answering
for one account while a different address is configured, and a change snapshot
from the previous account being diffed against the current one.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from benepass import api, auth, cache, cli_submit, session
from benepass.cli import app

runner = CliRunner()


def test_logout_keeps_the_account_address(state_dir: Path) -> None:
    """Otherwise the next `login` claims this is the machine's first one."""
    session.save(email="user@example.com", refresh_token="tok", workspace_id="ws")
    cache.save({"benefits": {}})

    result = runner.invoke(app, ["logout"])

    assert result.exit_code == 0
    assert session.refresh_token() is None
    assert session.load().get("workspace_id") is None
    assert session.email() == "user@example.com"


def test_logout_drops_the_change_snapshot(state_dir: Path) -> None:
    """It holds the account's balances and merchant names, not just ids."""
    session.save(email="user@example.com", refresh_token="tok")
    cache.save({"benefits": {"b": {"name": "Wellness"}}})

    runner.invoke(app, ["logout"])

    assert cache.load() == {}


def test_switching_accounts_clears_the_previous_snapshot(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept snapshot reports every pot of the old account as REMOVED."""
    session.save(email="old@example.com", refresh_token="old-tok")
    cache.save({"benefits": {"b": {"name": "Wellness"}}})
    session.set_pending("sess", "CUSTOM_CHALLENGE", "new@example.com", int(time.time()))
    monkeypatch.setattr(api, "respond_to_challenge", lambda *a, **k: "new-tok")

    assert auth.complete("123456") == "new@example.com"
    assert cache.load() == {}
    assert session.load().get("email") == "new@example.com"


def test_a_configured_address_that_is_not_the_tokens_owner_refuses(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acting as the stored account would file a claim against the wrong one."""
    session.save(email="old@example.com", refresh_token="tok", workspace_id="ws")
    monkeypatch.setenv("BENEPASS_EMAIL", "new@example.com")

    with pytest.raises(auth.LoginRequired) as caught:
        auth.client()

    message = str(caught.value)
    assert "old@example.com" in message and "new@example.com" in message


def test_login_refuses_an_email_that_contradicts_the_environment(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It would succeed, then every later command would exit 3 on the mismatch."""
    monkeypatch.setenv("BENEPASS_EMAIL", "env@example.com")
    monkeypatch.setattr(
        api, "start_email_code_login", lambda address: pytest.fail("code requested")
    )

    result = runner.invoke(app, ["login", "--email", "flag@example.com"])

    assert result.exit_code == 1
    assert "flag@example.com" in result.stderr
    assert "env@example.com" in result.stderr
    assert session.pending() is None


def test_login_accepts_an_email_that_matches_the_environment(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agreeing is not a conflict — only a contradiction is."""
    monkeypatch.setenv("BENEPASS_EMAIL", "user@example.com")
    monkeypatch.setattr(auth, "interactive", lambda: False)
    monkeypatch.setattr(api, "start_email_code_login", lambda address: {"Session": "s"})

    result = runner.invoke(app, ["login", "--email", "user@example.com"])

    assert result.exit_code == 0
    assert session.pending() is not None


def test_matching_addresses_do_not_trip_the_guard(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.save(email="user@example.com", refresh_token="tok", workspace_id="ws")
    monkeypatch.setenv("BENEPASS_EMAIL", "user@example.com")

    assert auth.client().workspace_id == "ws"


def test_a_malformed_api_body_is_a_message_not_a_traceback(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_submit, "run", lambda fn, *a, **k: None)

    result = runner.invoke(
        app, ["api", "/v2/me/expenses/", "-X", "POST", "--body", "{"]
    )

    assert result.exit_code == 1
    assert "--body is not valid JSON" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_quoted_cognito_response_carries_no_token_material() -> None:
    """These branches fire precisely when Cognito answered WITH tokens."""
    rendered = api.redacted(
        {
            "AuthenticationResult": {"AccessToken": "eyJ" + "A" * 900},
            "Session": "sess-" + "B" * 200,
            "ChallengeName": "CUSTOM_CHALLENGE",
        }
    )

    assert "eyJ" not in rendered
    assert "sess-" not in rendered
    assert json.loads(rendered)["ChallengeName"] == "CUSTOM_CHALLENGE"


def test_the_otp_window_starts_a_second_early(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second-granularity mail filter must not exclude the code that arrived."""
    seen: dict[str, Any] = {}

    monkeypatch.setattr(api, "start_email_code_login", lambda address: {"Session": "s"})
    monkeypatch.setattr(auth, "interactive", lambda: False)
    monkeypatch.setenv("BENEPASS_OTP_COMMAND", "true")

    def fake_poll(command: str, address: str, since_epoch: int) -> str:
        seen["since"] = since_epoch
        return "123456"

    monkeypatch.setattr(auth, "poll_otp_command", fake_poll)
    monkeypatch.setattr(auth, "complete", lambda code: "user@example.com")

    auth.login("user@example.com")

    assert seen["since"] <= int(time.time()) - 1
