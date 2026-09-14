"""Benepass API client: AWS Cognito email-OTP auth + the /v2/me REST surface.

Endpoint constants and the request shapes are the ones used by Benepass's own
`employee-web` app (also mapped by the MIT-licensed domdomegg/benepass-mcp).
None of them are secrets. The refresh token IS a secret and never leaves
`session.py`'s state file — it is not accepted as a CLI argument and is never
printed, because an agent session keeps a lasting copy of everything it runs.

Unofficial and unsupported: Benepass publishes no public API, so any of this can
break without notice.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from . import __version__

CLIENT_ID = "6l7jeu4r44kgndgeab4aot355m"
COGNITO_IDP_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
COGNITO_TOKEN_URL = "https://cognito.benefitsapi.com/oauth2/token"
API_BASE_URL = "https://api.benefitsapi.com"

USER_AGENT = (
    f"benepass-cli/{__version__} (+https://github.com/mcinteerj/benepass-plugin)"
)
TIMEOUT = 30.0


class BenepassError(RuntimeError):
    """Actionable API/auth failure.

    `status_code` is the HTTP status when the failure came back as a response,
    and None when nothing was answered at all (DNS, TLS, timeout, refused
    connection). Callers that treat a 4xx as "your input was wrong" have to be
    able to tell it from a 5xx or a dead network, which say nothing about the
    input — see `respond_to_challenge`.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthExpired(BenepassError):
    """The stored refresh token was rejected — a fresh login is required."""


class ChallengeRejected(BenepassError):
    """Cognito refused the one-time code.

    A CUSTOM_AUTH Session is single-use: answering it wrongly either fails
    outright or returns a BRAND NEW Session with the challenge re-issued. Either
    way the Session that was sent is dead, so the caller must replace or drop the
    stored pending challenge — re-sending it makes the next attempt fail with an
    error that blames a code which may well be correct.

    `challenge_session` carries the replacement when Cognito issued one.
    """

    def __init__(self, message: str, challenge_session: str | None = None) -> None:
        super().__init__(message, 400)
        self.challenge_session = challenge_session


# --------------------------------------------------------------------------
# Cognito (login)
# --------------------------------------------------------------------------


def _cognito_idp(target: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = httpx.post(
            COGNITO_IDP_URL,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
            },
            content=json.dumps(body),
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        # Login is the first command anyone runs, and a captive portal, a VPN or
        # a proxy is a routine way for it to fail. A raw traceback here is not a
        # useful thing to hand a person — and carries no status, so nothing
        # downstream can mistake it for a refused code.
        raise BenepassError(f"could not reach Cognito ({target}): {exc}") from exc
    if resp.status_code >= 400:
        raise BenepassError(
            f"Cognito {target} failed (HTTP {resp.status_code}): {resp.text[:400]}",
            resp.status_code,
        )
    try:
        parsed: Any = resp.json()
    except ValueError as exc:
        raise BenepassError(
            f"Cognito {target} returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise BenepassError(
            f"Cognito {target} returned {type(parsed).__name__}, not an object"
        )
    return parsed


def redacted(result: dict[str, Any]) -> str:
    """A Cognito response rendered for an error message, with the credentials out.

    The branches that quote a response are precisely the ones where Cognito
    answered with tokens instead of the challenge that was expected, so the
    object in hand is the one carrying `AuthenticationResult` — and the message
    goes to stderr, which an agent session archives permanently. Redact rather
    than rely on the truncation to cut the JWT off somewhere harmless.
    """
    safe = {
        k: v for k, v in result.items() if k not in ("AuthenticationResult", "Session")
    }
    if "AuthenticationResult" in result:
        safe["AuthenticationResult"] = "<redacted>"
    if "Session" in result:
        safe["Session"] = "<redacted>"
    return json.dumps(safe)[:400]


# Benepass's Cognito custom-auth flow has TWO challenge rounds, not one. The
# first round is a `protocol_selector`: it asks which passwordless method to use
# and sends NO email until it is answered. Answering it with "email_code_v0" is
# what actually triggers the code email. Skipping this round yields a
# valid-looking Session and a silent no-email failure. Confirmed by capturing
# the real signon.benefitsapi.com login in a browser's network inspector.
EMAIL_CODE_PROTOCOL = "email_code_v0"


def initiate_auth(email: str) -> dict[str, Any]:
    """Start CUSTOM_AUTH. Returns the first challenge (usually the protocol selector)."""
    result = _cognito_idp(
        "InitiateAuth",
        {
            "AuthFlow": "CUSTOM_AUTH",
            "ClientId": CLIENT_ID,
            "AuthParameters": {"USERNAME": email},
        },
    )
    if not result.get("Session"):
        raise BenepassError(
            f"Cognito InitiateAuth returned no Session: {redacted(result)}"
        )
    return result


def select_email_code(email: str, challenge: dict[str, Any]) -> dict[str, Any]:
    """Answer the protocol_selector round, which is what sends the OTP email.

    Echoes the challenge parameters back the way the web client does — Benepass's
    Lambda reads `challenge_type`/`schema_version` out of the response.
    """
    params = challenge.get("ChallengeParameters") or {}
    responses = {
        **{k: v for k, v in params.items() if k != "USERNAME"},
        "USERNAME": email,
        "ANSWER": EMAIL_CODE_PROTOCOL,
    }
    result = _cognito_idp(
        "RespondToAuthChallenge",
        {
            "ClientId": CLIENT_ID,
            "ChallengeName": challenge.get("ChallengeName", "CUSTOM_CHALLENGE"),
            "Session": challenge["Session"],
            "ChallengeResponses": responses,
            "ClientMetadata": {
                "protocol": EMAIL_CODE_PROTOCOL,
                "schema_version": params.get("schema_version", "1"),
            },
        },
    )
    if not result.get("Session"):
        raise BenepassError(
            f"Protocol selection returned no Session: {redacted(result)}"
        )
    return result


def start_email_code_login(email: str) -> dict[str, Any]:
    """Full 'send me a code' step: initiate, then select email-code if asked."""
    challenge = initiate_auth(email)
    params = challenge.get("ChallengeParameters") or {}
    if params.get("challenge_type") == "protocol_selector":
        challenge = select_email_code(email, challenge)
    return challenge


def respond_to_challenge(
    email: str, otp: str, session: str, challenge_name: str = "CUSTOM_CHALLENGE"
) -> str:
    """Answer the one-time-code challenge; returns the durable refresh token."""
    try:
        result = _cognito_idp(
            "RespondToAuthChallenge",
            {
                "ChallengeName": challenge_name,
                "ClientId": CLIENT_ID,
                "Session": session,
                "ChallengeResponses": {"USERNAME": email, "ANSWER": otp},
            },
        )
    except BenepassError as exc:
        # Only a 4xx (NotAuthorizedException and friends) means the code was
        # refused and the Session is spent. A 5xx, or a transport failure with
        # no status at all, says nothing about the code — reporting those as a
        # rejection would blame a correct code AND discard a challenge the
        # caller could still have retried.
        if exc.status_code is None or exc.status_code >= 500:
            raise
        raise ChallengeRejected(str(exc)) from exc
    auth = result.get("AuthenticationResult") or {}
    token = auth.get("RefreshToken")
    if not token:
        # Cognito re-issues the challenge with a fresh Session when there are
        # attempts left; carry it back so a retry can use it.
        raise ChallengeRejected(
            "Login was not completed — the code may have been wrong or the challenge expired. "
            f"Cognito returned: {redacted(result)}",
            result.get("Session"),
        )
    return str(token)


def exchange_refresh_token(refresh_token: str) -> tuple[str, int]:
    """Swap the refresh token for a short-lived access token. Returns (token, expires_in)."""
    try:
        resp = httpx.post(
            COGNITO_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        # Called from inside Client.request's try block today, which would
        # mislabel this as a failure of whatever path was being requested.
        raise BenepassError(
            f"could not reach the Benepass token endpoint: {exc}"
        ) from exc
    if resp.status_code == 400:
        raise AuthExpired(
            "Refresh token rejected (invalid_grant) — it expired or was revoked."
        )
    if resp.status_code >= 400:
        raise BenepassError(
            f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:400]}"
        )
    # Parse defensively: an unhandled KeyError/ValueError here would escape as a
    # traceback, and typer's rich traceback prints frame locals — which in this
    # frame means the refresh token, permanently, into whatever log is watching.
    try:
        body = resp.json()
    except ValueError as exc:
        raise BenepassError(
            f"token endpoint returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
        ) from exc
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise BenepassError(
            "token endpoint returned no access_token — Benepass may have changed the "
            "login flow; try `benepass login --force`."
        )
    return str(token), int(body.get("expires_in", 3600))


# --------------------------------------------------------------------------
# REST client
# --------------------------------------------------------------------------


# A path must be rooted at a single "/". Anything else can retarget the host the
# request goes to — "@evil.tld/v2/me/" parses as userinfo + a new host, and
# ".evil.tld/v2/me/" simply extends the hostname — and the Bearer token is
# attached to whatever host the URL names. `benepass api <path>` takes this
# straight from the caller, so validate rather than trust.
SAFE_PATH_RE = re.compile(r"^/(?![/\\])")


def build_url(path: str) -> str:
    """Absolute URL for an API path, refusing anything that could leave the host."""
    if not SAFE_PATH_RE.match(path) or "\\" in path:
        raise BenepassError(
            f"invalid API path {path!r} — it must start with a single '/', "
            "e.g. /v2/me/accounts/"
        )
    url = f"{API_BASE_URL}{path}"
    if httpx.URL(url).host != httpx.URL(API_BASE_URL).host:
        raise BenepassError(
            f"invalid API path {path!r} — it would send the request to "
            f"{httpx.URL(url).host!r} instead of Benepass."
        )
    return url


class Client:
    """Authenticated Benepass API client. Caches the access token in-process."""

    def __init__(self, refresh_token: str, workspace_id: str | None = None) -> None:
        self._refresh_token = refresh_token
        self.workspace_id = workspace_id
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _token(self) -> str:
        if self._access_token and self._expires_at - time.time() > 60:
            return self._access_token
        token, expires_in = exchange_refresh_token(self._refresh_token)
        self._access_token = token
        self._expires_at = time.time() + expires_in
        return token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self._token()}",
            "x-benepass-client": "employee-web",
            "x-benepass-platform": "web",
        }
        if self.workspace_id:
            headers["x-benepass-workspace-id"] = self.workspace_id
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        extra = {"Content-Type": "application/json"} if json_body is not None else None
        url = build_url(path)
        try:
            resp = httpx.request(
                method,
                url,
                headers=self._headers(extra),
                params=clean or None,
                content=json.dumps(json_body) if json_body is not None else None,
                files=files,
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            # Unattended callers report whatever this raises; a raw traceback from
            # a DNS blip is not a useful thing to hand a person.
            raise BenepassError(f"could not reach Benepass ({path}): {exc}") from exc
        if resp.status_code == 401:
            raise AuthExpired("Benepass rejected the access token (401).")
        if resp.status_code == 403 and "Workspace required" in resp.text:
            raise BenepassError(
                "Benepass rejected the stored workspace. Run `benepass workspaces` "
                "to see yours, then `benepass logout` and log in again to "
                "re-resolve it."
            )
        if resp.status_code >= 400:
            raise BenepassError(
                f"Benepass API error {resp.status_code} on {path}: {resp.text[:500]}"
            )
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise BenepassError(
                f"Benepass returned non-JSON on {path} (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from exc

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, json_body=body)

    # -- convenience wrappers over the endpoints the CLI uses ---------------

    def workspaces(self) -> list[dict[str, Any]]:
        return rows(self.get("/v2/me/workspaces/"))

    def accounts(self) -> list[dict[str, Any]]:
        return rows(self.get("/v2/me/accounts/"))

    def transactions(
        self,
        limit: int = 20,
        offset: int = 0,
        benefit: str | None = None,
        since: str | None = None,
        until: str | None = None,
        kind: str | None = None,
        search: str | None = None,
    ) -> Any:
        """List transactions.

        The server honours `since`/`until` (YYYY-MM-DD), `type` (one of card,
        reimbursement, employer_contribution, expiration — single value only) and
        `search` (merchant substring). It IGNORES anything it doesn't recognise and
        still returns 200, so an unsupported filter looks exactly like a filter that
        matched everything: prove any new one by the row count changing.
        """
        return self.get(
            "/v2/me/transactions/",
            {
                "limit": limit,
                "offset": offset,
                "benefit": benefit,
                "since": since,
                "until": until,
                "type": kind,
                "search": search,
            },
        )

    def transaction(self, txn_id: str) -> Any:
        return self.get(f"/v2/me/transactions/{txn_id}/")

    def currencies(self) -> list[dict[str, Any]]:
        return rows(self.get("/v2/me/currencies/", {"page_size": 200}))

    def next_events(self, account_id: str, until: str) -> list[dict[str, Any]]:
        """Scheduled contributions/expirations for an account, up to `until` (YYYY-MM-DD)."""
        return rows(
            self.get(f"/v2/me/accounts/{account_id}/next-events/", {"until": until})
        )

    def max_rollover(self, account_id: str) -> dict[str, Any]:
        """How much of a balance survives the next expiration event."""
        data = self.get(f"/v2/me/accounts/{account_id}/schedules/max-rollover-amount/")
        return (data or {}).get("data", data) or {}

    def substantiation(self, benefit_id: str) -> Any:
        return self.get(f"/v2/me/benefits/{benefit_id}/substantiation-requirements/")

    def upload_receipt(self, filename: str, data: bytes, mime: str) -> Any:
        return self.request(
            "POST", "/v2/me/claims/uploads/", files={"file": (filename, data, mime)}
        )

    def create_expense(self, body: dict[str, Any]) -> Any:
        return self.post("/v2/me/expenses/", body)


def rows(payload: Any) -> list[dict[str, Any]]:
    """Benepass mixes Stripe-style {data:[]} and DRF-style {results:[]} envelopes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []
