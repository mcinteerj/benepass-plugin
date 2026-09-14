"""Login: request the emailed one-time code, then exchange it for a session.

Benepass has no passwords — signing in means answering a 6-digit code that is
emailed to the account address. Getting that code out of your inbox is the one
thing this CLI cannot do for you, so there are three ways to finish the flow:

1. ``BENEPASS_OTP_COMMAND`` — a shell command that prints the code. If it is
   set, `benepass login` polls it and completes without anyone typing anything,
   which is what makes the tool usable from an unattended job.
2. An interactive terminal — `benepass login` prompts for the code inline.
3. Anything else (an agent, a pipe, a cron job with no OTP command) — `login`
   prints where the code was sent and exits 0; a second call,
   `benepass login --code 123456`, finishes it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from . import api, cache, session

OTP_SENDER = "donotreply@getbenepass.com"
OTP_SUBJECT = "Your Benepass Login Code"

# Pulling the code out of arbitrary mail output, in order of confidence.
#
# The code is SIX digits, and taking the first 4-8 digit run instead is wrong in
# the most ordinary case there is: a mail client prints a Date header, and its
# four-digit year matches before the code does. So does a copyright footer. The
# year then goes to Cognito, is refused, and the error blames the code while the
# command was finding the right email every time.
#
# Lookarounds rather than \b so a longer number (an order id, an epoch) is never
# sliced into something that looks like a code.
SIX = r"(?<!\d)(\d{6})(?!\d)"
CODE_NEAR_WORD_RE = re.compile(r"code\D{0,20}?" + SIX, re.IGNORECASE)
CODE_RE = re.compile(SIX)
# Last resort only, for a command that prints nothing but the digits and a
# provider that is not using six of them.
CODE_WIDE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

POLL_TIMEOUT = 120
POLL_INTERVAL = 5
OTP_COMMAND_TIMEOUT = 60
# Cognito custom-auth challenge sessions live about three minutes. Treat
# anything older as dead rather than sending a code Cognito will reject with a
# message that blames the code.
PENDING_TTL = 300


class LoginError(api.BenepassError):
    """Login could not be completed."""


class LoginRequired(LoginError):
    """No usable session, and this process cannot get a code by itself.

    The CLI exits 3 on this, so a caller can tell "you must log in" apart from
    every other failure.
    """


def extract_code(text: str) -> str | None:
    """Pull a login code out of arbitrary command output.

    Six digits next to the word "code" beats six digits anywhere, which beats a
    4-8 digit run. See the patterns above for why the widest match is last.
    """
    match = CODE_NEAR_WORD_RE.search(text) or CODE_RE.search(text)
    if match is None:
        match = CODE_WIDE_RE.search(text)
    return match.group(1) if match else None


def _run_otp_command(
    command: str, address: str, since_epoch: int, budget: float = OTP_COMMAND_TIMEOUT
) -> str | None:
    """Run the user's OTP command once. Non-zero exit means "not yet"."""
    env = {
        **os.environ,
        "BENEPASS_OTP_SINCE": str(since_epoch),
        "BENEPASS_EMAIL": address,
    }
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(0.1, min(OTP_COMMAND_TIMEOUT, budget)),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return extract_code(proc.stdout)


def poll_otp_command(command: str, address: str, since_epoch: int) -> str | None:
    """Poll the OTP command until it yields a code or POLL_TIMEOUT elapses.

    Sleeps first: the email has to arrive, and asking immediately mostly finds
    the previous code if the command does not filter on BENEPASS_OTP_SINCE.

    POLL_TIMEOUT is a real ceiling on the whole call, not on the last attempt's
    start. Testing the deadline before the sleep and then giving the command a
    flat 60s let an iteration entered at 119s run to ~180s — so a caller that
    budgets the documented two minutes kills the login mid-poll, which reads as
    a hung CLI rather than a slow mailbox.
    """
    deadline = time.time() + POLL_TIMEOUT
    while True:
        time.sleep(POLL_INTERVAL)
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        code = _run_otp_command(command, address, since_epoch, budget=remaining)
        if code:
            return code


def interactive() -> bool:
    """Is there a human at a terminal who could type the code?"""
    return sys.stdin.isatty()


def can_autologin() -> bool:
    """Can this process obtain a code without a second invocation?"""
    return bool(os.environ.get("BENEPASS_OTP_COMMAND")) or interactive()


def request_code(address: str) -> None:
    """Ask Benepass to email a code, and remember the challenge it opened."""
    challenge = api.start_email_code_login(address)
    session.set_pending(
        challenge["Session"],
        str(challenge.get("ChallengeName", "CUSTOM_CHALLENGE")),
        address,
        int(time.time()),
    )
    # Remember the address the moment it is used, not only once a login
    # succeeds. A late or mistyped code sends the user back to a bare
    # `benepass login`, and that retry must not fail with "no email" over an
    # address they already supplied. Kept under its own key: `email` means "the
    # account we are logged in as", and this one is not yet proven to exist.
    session.save(login_email=address)


def code_sent_message(address: str) -> str:
    return (
        f"Login code sent to {address} (from {OTP_SENDER}, subject "
        f"{OTP_SUBJECT!r}). Read it from your inbox, then run: "
        "benepass login --code 123456"
    )


def complete(code: str) -> str:
    """Answer the pending challenge with `code`. Returns the email logged in as."""
    digits = extract_code(code)
    if not digits:
        raise LoginError(f"{code!r} is not a login code — expected 4-8 digits.")

    challenge = session.pending()
    if not challenge:
        raise LoginRequired(
            "No login is in progress. Run `benepass login` first, then "
            "`benepass login --code <code>` with the code that is emailed to you."
        )
    age = int(time.time()) - int(challenge.get("requested_at") or 0)
    if age > PENDING_TTL:
        session.clear_pending()
        when = f"{age // 60} minutes old" if age < 86400 else "from an earlier session"
        raise LoginRequired(
            f"That login attempt is {when}, and Benepass has expired it (a "
            "challenge lasts about 3 minutes). Run `benepass login` again for a "
            "fresh code."
        )

    address = str(challenge.get("email") or session.email() or "")
    challenge_name = str(challenge.get("ChallengeName", "CUSTOM_CHALLENGE"))
    try:
        token = api.respond_to_challenge(
            address, digits, str(challenge["Session"]), challenge_name
        )
    except api.ChallengeRejected as exc:
        # The Session we just sent is spent either way. Leaving it in place made
        # the retry — with the RIGHT code — fail identically and blame the code.
        if exc.challenge_session:
            session.set_pending(
                exc.challenge_session,
                challenge_name,
                address,
                int(challenge.get("requested_at") or time.time()),
            )
            raise LoginError(
                "That code was refused. Benepass re-issued the challenge, so run "
                "`benepass login --code <code>` again with the code from the same "
                "email — or `benepass login` for a fresh one."
            ) from exc
        session.clear_pending()
        raise LoginRequired(
            "That code was refused and the login attempt is spent. Run "
            "`benepass login` for a fresh code, then `benepass login --code <code>`."
        ) from exc
    session.clear_pending()
    previous = session.load().get("email")
    if previous and previous != address:
        # Switching accounts. `save` merges, so the old account's workspace id
        # would survive and be sent with the new token — 403 on every request,
        # with nothing but `benepass logout` to recover. Start clean instead;
        # the new workspace is resolved on the next command.
        session.clear()
        # The change snapshot is per-account and carries no account id, so
        # leaving it makes the first `changes` diff this account against the
        # previous one: every pot reported NEW, every pot of the old account
        # reported REMOVED, which reads as an employer policy change.
        cache.clear()
    session.save(refresh_token=token, email=address, logged_in_at=int(time.time()))
    return address


def login(address: str, verbose: bool = False) -> bool:
    """Start a login, and finish it if this process can get the code itself.

    Returns True when a session was stored, False when a code was emailed and
    `login --code <code>` is still needed. Never returns or prints the token.
    """
    # One second of slack: a mail-side date filter is second-granularity, and
    # may be exclusive, so starting a hair early avoids excluding the very code
    # that arrived. Without it a clock a second behind burns the whole poll and
    # the error blames BENEPASS_OTP_COMMAND.
    started = int(time.time()) - 1
    request_code(address)
    if verbose:
        print(f"Login code requested for {address}.")

    otp_command = os.environ.get("BENEPASS_OTP_COMMAND")
    if otp_command:
        if verbose:
            print(f"Waiting up to {POLL_TIMEOUT}s for BENEPASS_OTP_COMMAND ...")
        code = poll_otp_command(otp_command, address, started)
        if not code:
            raise LoginError(
                f"BENEPASS_OTP_COMMAND produced no login code within {POLL_TIMEOUT}s. "
                f"Check it reads mail from {OTP_SENDER} and prints the digits, or "
                "run `benepass login --code <code>` by hand."
            )
        complete(code)
        return True

    if interactive():
        code = input("Login code: ").strip()
        complete(code)
        return True

    return False


def _relogin(address: str, verbose: bool) -> None:
    """Re-login mid-command, or explain why the caller has to do it."""
    if not can_autologin():
        raise LoginRequired(
            "Not logged in — this machine has no usable Benepass session. Run "
            "`benepass login`, read the code from your email, then "
            "`benepass login --code <code>`."
        )
    if verbose:
        print("Stored session expired; logging in again...")
    login(address, verbose=verbose)


def client(
    workspace_id: str | None = None, auto: bool = True, verbose: bool = False
) -> api.Client:
    """Build an authenticated client, logging in first if needed."""
    address = session.email()
    if not address:
        raise LoginRequired(
            "No Benepass email configured. Set BENEPASS_EMAIL, or run: "
            "benepass login --email you@example.com"
        )

    # The stored token, and the workspace id stored beside it, belong to ONE
    # account. Only `login` used to compare them, so exporting a second
    # account's address without logging in as it answered every read from the
    # first account and would have filed a claim there — irreversibly, with a
    # preview that looked coherent because the workspace travelled with the
    # token. Refuse to act as an account nobody asked for.
    mismatch = session.account_mismatch()
    if mismatch and session.refresh_token():
        configured, owner = mismatch
        raise LoginRequired(
            f"The stored session belongs to {owner}, but {configured} is "
            "configured (BENEPASS_EMAIL). Run `benepass login` to log in as "
            f"{configured}, or unset BENEPASS_EMAIL to keep using {owner}."
        )

    if not session.refresh_token():
        if not auto:
            raise LoginRequired("Not logged in. Run: benepass login")
        _relogin(address, verbose)

    token = session.refresh_token()
    if token is None:  # pragma: no cover — login() raises rather than returning
        raise LoginRequired("Login did not produce a session. Run: benepass login")
    workspace = workspace_id or session.load().get("workspace_id")
    cli = api.Client(token, workspace)

    if not workspace:
        cli.workspace_id = _default_workspace(cli, auto, verbose, address)
        session.save(workspace_id=cli.workspace_id)
    return cli


def _default_workspace(
    cli: api.Client, auto: bool, verbose: bool, address: str
) -> str | None:
    """Pick the employment workspace, re-logging in once if the token has expired."""
    try:
        rows = cli.workspaces()
    except api.AuthExpired:
        if not auto:
            raise
        session.clear_token()
        _relogin(address, verbose)
        token = session.refresh_token()
        if token is None:  # pragma: no cover
            raise LoginRequired("Login did not produce a session.") from None
        cli._refresh_token = token  # noqa: SLF001 — same module family, avoids rebuilding
        cli._access_token = None  # noqa: SLF001
        rows = cli.workspaces()

    employment = next((w for w in rows if w.get("type") == "employment"), None)
    chosen = employment or (rows[0] if rows else None)
    if not chosen:
        return None
    chosen_id = chosen.get("id")
    return str(chosen_id) if chosen_id else None
