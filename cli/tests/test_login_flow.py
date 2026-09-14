"""The two-step login: request a code, then complete it from another process.

No network — the Cognito calls are stubbed. What is worth testing is the state
that has to survive between `benepass login` and `benepass login --code`, and
the refusal paths that would otherwise produce a misleading error from Cognito.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from benepass import api, auth, session


def _stub_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_start(address: str) -> dict[str, Any]:
        return {"Session": "sess-abc", "ChallengeName": "CUSTOM_CHALLENGE"}

    monkeypatch.setattr(api, "start_email_code_login", fake_start)


def test_non_interactive_login_emails_a_code_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)

    assert auth.login("user@example.com") is False
    pending = session.pending()
    assert pending is not None
    assert pending["Session"] == "sess-abc"
    assert pending["email"] == "user@example.com"
    assert session.refresh_token() is None


def test_completing_the_pending_challenge_stores_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")

    seen: dict[str, str] = {}

    def fake_respond(
        address: str, otp: str, challenge_session: str, challenge_name: str
    ) -> str:
        seen.update(
            address=address, otp=otp, session=challenge_session, name=challenge_name
        )
        return "refresh-token-value"

    monkeypatch.setattr(api, "respond_to_challenge", fake_respond)

    assert auth.complete("123456") == "user@example.com"
    assert seen == {
        "address": "user@example.com",
        "otp": "123456",
        "session": "sess-abc",
        "name": "CUSTOM_CHALLENGE",
    }
    assert session.refresh_token() == "refresh-token-value"
    # The challenge is single-use; leaving it behind invites a stale retry.
    assert session.pending() is None


def test_completing_without_a_pending_login_says_so() -> None:
    """LoginRequired, not a bare LoginError: the CLI turns it into exit 3."""
    with pytest.raises(auth.LoginRequired, match="No login is in progress"):
        auth.complete("123456")


def test_a_stale_challenge_is_refused_rather_than_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.set_pending(
        "sess-old",
        "CUSTOM_CHALLENGE",
        "user@example.com",
        int(time.time()) - auth.PENDING_TTL - 1,
    )
    with pytest.raises(auth.LoginRequired, match="expired it"):
        auth.complete("123456")
    assert session.pending() is None


def test_a_non_code_argument_is_refused() -> None:
    with pytest.raises(auth.LoginError, match="not a login code"):
        auth.complete("please")


def test_otp_command_completes_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_challenge(monkeypatch)
    monkeypatch.setenv("BENEPASS_OTP_COMMAND", "echo 'code: 246813'")
    monkeypatch.setattr(auth, "POLL_INTERVAL", 0)
    monkeypatch.setattr(api, "respond_to_challenge", lambda *a: "refresh-token-value")

    assert auth.login("user@example.com") is True
    assert session.refresh_token() == "refresh-token-value"


def test_otp_command_gets_the_email_and_request_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_challenge(monkeypatch)
    out = tmp_path / "env.txt"
    monkeypatch.setenv(
        "BENEPASS_OTP_COMMAND",
        f'printf "%s %s\\n" "$BENEPASS_EMAIL" "$BENEPASS_OTP_SINCE" > {out}; echo 135790',
    )
    monkeypatch.setattr(auth, "POLL_INTERVAL", 0)
    monkeypatch.setattr(api, "respond_to_challenge", lambda *a: "refresh-token-value")

    auth.login("user@example.com")
    address, since = out.read_text().split()
    assert address == "user@example.com"
    assert abs(int(since) - int(time.time())) < 60


def test_a_failing_otp_command_is_not_a_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit means 'not yet' — its stdout must be ignored, not parsed."""
    _stub_challenge(monkeypatch)
    monkeypatch.setenv("BENEPASS_OTP_COMMAND", "echo 999999; exit 1")
    monkeypatch.setattr(auth, "POLL_INTERVAL", 0)
    monkeypatch.setattr(auth, "POLL_TIMEOUT", 0.1)

    with pytest.raises(auth.LoginError, match="no login code"):
        auth.login("user@example.com")
    assert session.refresh_token() is None


def test_the_poll_deadline_is_a_ceiling_on_the_whole_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow command entered just under the deadline must not triple the budget.

    The old loop tested the deadline BEFORE sleeping and then gave the command a
    flat OTP_COMMAND_TIMEOUT, so a documented 120s poll could run to ~180s.
    """
    monkeypatch.setattr(auth, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(auth, "POLL_TIMEOUT", 0.3)
    monkeypatch.setattr(auth, "OTP_COMMAND_TIMEOUT", 5)

    started = time.monotonic()
    assert auth.poll_otp_command("sleep 5", "user@example.com", 0) is None
    assert time.monotonic() - started < 2


def test_client_without_a_session_raises_login_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit-3 path: no session, and no way to get a code in this process."""
    monkeypatch.setattr(auth, "interactive", lambda: False)
    session.save(email="user@example.com")
    with pytest.raises(auth.LoginRequired):
        auth.client()


def test_clear_token_keeps_the_email_for_the_re_login() -> None:
    session.save(email="user@example.com", refresh_token="x", workspace_id="ws_1")
    session.clear_token()
    assert session.refresh_token() is None
    assert session.email() == "user@example.com"
    assert session.load()["workspace_id"] == "ws_1"


def test_a_refused_code_replaces_the_challenge_so_a_retry_can_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cognito burns the Session on a wrong answer and re-issues a new one.

    Keeping the old one made the retry — with the RIGHT code — fail identically,
    with a message that blamed the code.
    """
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")

    def reissue(*_: Any) -> str:
        raise api.ChallengeRejected("wrong code", "sess-second")

    monkeypatch.setattr(api, "respond_to_challenge", reissue)
    with pytest.raises(auth.LoginError, match="re-issued"):
        auth.complete("111111")

    pending = session.pending()
    assert pending is not None
    assert pending["Session"] == "sess-second"

    sent: dict[str, str] = {}

    def accept(
        address: str, otp: str, challenge_session: str, challenge_name: str
    ) -> str:
        sent.update(session=challenge_session)
        return "refresh-token-value"

    monkeypatch.setattr(api, "respond_to_challenge", accept)
    assert auth.complete("123456") == "user@example.com"
    assert sent["session"] == "sess-second"


def test_a_spent_challenge_is_cleared_and_asks_for_a_fresh_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No replacement Session means the attempt is dead: exit-3 territory."""
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")

    def refuse(*_: Any) -> str:
        raise api.ChallengeRejected("Cognito RespondToAuthChallenge failed (HTTP 400)")

    monkeypatch.setattr(api, "respond_to_challenge", refuse)
    with pytest.raises(auth.LoginRequired, match="fresh code"):
        auth.complete("111111")
    assert session.pending() is None


def test_the_address_is_remembered_before_the_first_login_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a late code must not be asked for the address again."""
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")

    assert session.email() == "user@example.com"
    # Still not an account we have logged in as, so the "already logged in"
    # check must not see it.
    assert session.load().get("email") is None
    assert session.refresh_token() is None

    # ... and it survives the stale-challenge path, which clears the pending
    # blob the address used to live inside.
    session.clear_pending()
    assert session.email() == "user@example.com"


def test_a_server_error_does_not_spend_the_pending_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx says nothing about the code: keep the challenge, blame the server."""
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")

    def boom(*_: Any) -> str:
        raise api.BenepassError("Cognito RespondToAuthChallenge failed (HTTP 503)", 503)

    monkeypatch.setattr(api, "respond_to_challenge", boom)
    with pytest.raises(api.BenepassError, match="503"):
        auth.complete("123456")

    pending = session.pending()
    assert pending is not None
    assert pending["Session"] == "sess-abc"


def test_an_unreachable_cognito_does_not_spend_the_pending_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same for a transport failure, which carries no status at all."""
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")

    def unreachable(*_: Any) -> str:
        raise api.BenepassError("could not reach Cognito (RespondToAuthChallenge)")

    monkeypatch.setattr(api, "respond_to_challenge", unreachable)
    with pytest.raises(api.BenepassError, match="could not reach"):
        auth.complete("123456")
    assert session.pending() is not None


def test_logging_in_as_another_address_drops_the_old_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merged save kept workspace_id, which 403s every call on the new token."""
    session.save(
        email="first@example.com", refresh_token="token-a", workspace_id="ws_first"
    )
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("second@example.com")
    monkeypatch.setattr(api, "respond_to_challenge", lambda *a: "token-b")

    assert auth.complete("123456") == "second@example.com"
    state = session.load()
    assert state["email"] == "second@example.com"
    assert state["refresh_token"] == "token-b"
    assert "workspace_id" not in state


def test_logging_in_again_as_the_same_address_keeps_the_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-resolving it is an extra API call on every routine re-login."""
    session.save(
        email="user@example.com", refresh_token="token-a", workspace_id="ws_only"
    )
    _stub_challenge(monkeypatch)
    monkeypatch.setattr(auth, "interactive", lambda: False)
    auth.login("user@example.com")
    monkeypatch.setattr(api, "respond_to_challenge", lambda *a: "token-b")

    auth.complete("123456")
    assert session.load()["workspace_id"] == "ws_only"
