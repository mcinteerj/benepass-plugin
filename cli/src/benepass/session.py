"""Session state: where the Benepass refresh token lives, and how it is guarded.

The refresh token is a live credential for your benefits account. It is stored
per-machine at ~/.config/benepass/session.json (mode 0600, in a 0700 directory,
created at that mode rather than narrowed afterwards) and deliberately not
in any shared secret store: it is cheaply re-mintable (one emailed code) and
per-machine state is a smaller blast radius than a synced secret.

It is never accepted as a CLI argument, never echoed, and never returned by any
function that feeds command output. Agent sessions archive every command they
run, so a token in argv or stdout is a lasting plaintext copy that only a
revocation undoes. (A one-time login code is different: single-use and valid for
minutes, so `login --code 123456` in argv is acceptable.)

The same file holds the pending Cognito challenge between `benepass login` and
`benepass login --code <code>`, because those are two separate processes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path.home() / ".config" / "benepass"


def state_dir() -> Path:
    """Where per-user state lives: `$BENEPASS_STATE_DIR`, else ~/.config/benepass.

    Two values that look like a setting and are not.

    **Empty is unset.** `os.environ.get(name, default)` hands back `""` for an
    exported-but-empty variable — the ordinary result of a crontab line or a
    launchd plist referencing something the user never set — and `Path("")` is
    `.`, which would put the refresh token, and a 0700 chmod, in whatever
    directory the command happened to run from. Every shell block in the skills
    already reads it as `${BENEPASS_STATE_DIR:-$HOME/.config/benepass}`, where
    empty means the default, so this keeps the CLI and those blocks pointing at
    the same directory rather than splitting the session from the preferences.

    **Relative is refused, not resolved.** It would mean a different directory
    per working directory: a session saved by one command and "login needed"
    from the next.

    **A leading `~` is refused too, rather than expanded.** Expanding it here
    would split the state directory in two: the skills read the variable as
    `${BENEPASS_STATE_DIR:-…}`, and parameter expansion never expands a tilde,
    so `~/bp-state` would put session.json in `$HOME/bp-state` while
    PREFERENCES.md and sweep.json landed in a directory literally named `~`
    under whatever the run's working directory was. The error names the
    expanded path so the fix is a copy-paste — including from a crontab, which
    takes the rest of its assignment line literally.
    """
    raw = (os.environ.get("BENEPASS_STATE_DIR") or "").strip()
    if not raw:
        return DEFAULT_STATE_DIR
    path = Path(raw)
    expanded = path.expanduser()
    if raw.startswith("~") and expanded.is_absolute():
        print(
            f"benepass: BENEPASS_STATE_DIR must be an absolute path, got {raw!r} — "
            "the skills read it as ${BENEPASS_STATE_DIR:-$HOME/.config/benepass}, "
            "where the shell leaves a leading `~` unexpanded, so your preferences "
            "and your session would end up in different directories. Use "
            f"{expanded} instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not path.is_absolute():
        print(
            f"benepass: BENEPASS_STATE_DIR must be an absolute path, got {raw!r} — "
            "a relative one would put your session in whatever directory each "
            "command runs from. Unset it to use ~/.config/benepass.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return path


STATE_DIR = state_dir()
STATE_FILE = STATE_DIR / "session.json"

PENDING_KEY = "pending_challenge"


def load() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        parsed: Any = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


DIR_MODE = stat.S_IRWXU  # 0700
FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


def ensure_dir() -> None:
    """Make the state directory, and keep it 0700.

    `mkdir(mode=...)` only applies to a directory it creates, so an existing
    0755 directory (the default umask) is narrowed explicitly.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if stat.S_IMODE(STATE_DIR.stat().st_mode) != DIR_MODE:
        STATE_DIR.chmod(DIR_MODE)


# O_NOFOLLOW is POSIX; Windows Python does not define it, and there the
# symlink-planting case it defends against needs privileges anyway.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def write_private(path: Path, text: str) -> None:
    """Write `text` so the file is never observable at any mode but 0600.

    `write_text` + `chmod` creates the file at the umask's mode — 0644 on a
    default umask — and only narrows it afterwards, so the credential is
    world-readable for the instant in between. Creating the descriptor with the
    mode closes that window, and renaming into place means a crash or a full
    disk can never leave a half-written file that parses as "no session".

    O_EXCL|O_NOFOLLOW closes the other hole: the temporary path is fixed and
    predictable, so on a shared BENEPASS_STATE_DIR a symlink planted there
    before first use would be FOLLOWED — the token written through it, at the
    target's mode, outside the 0700 directory. Create the temp file or fail, and
    never write through a link.
    """
    ensure_dir()
    if path.is_symlink():
        raise OSError(f"refusing to write {path}: it is a symlink, not a state file")
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, FILE_MODE)
    except FileExistsError as exc:
        raise OSError(
            f"refusing to write {tmp}: it reappeared between removal and creation"
        ) from exc
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write(state: dict[str, Any]) -> None:
    write_private(STATE_FILE, json.dumps(state, indent=2))


def save(**fields: Any) -> None:
    """Merge fields into the state file, keeping it 0600."""
    state = load()
    state.update({k: v for k, v in fields.items() if v is not None})
    _write(state)


def clear(keep_account: bool = False) -> None:
    """Forget the stored session.

    `keep_account=True` is the logout path: the credential goes, the account
    ADDRESS stays. Dropping it too made the next `benepass login` fail with "no
    email — this is the first login on this machine", which is both false and a
    broken promise, and it dead-ended the CLI's own advice to log out and back
    in to re-resolve a rejected workspace. It is kept under `login_email`
    because `email` means "the account we are logged in as", and after a logout
    we are not. The account-switch path leaves it False on purpose: there the
    point is to forget the previous account entirely.
    """
    address = None
    if keep_account:
        state = load()
        address = state.get("email") or state.get("login_email")
    STATE_FILE.unlink(missing_ok=True)
    if address:
        save(login_email=str(address))


def clear_token() -> None:
    """Drop only the expired credential, keeping the email and workspace.

    An expiry mid-command triggers a re-login, and that re-login needs to know
    which address to use — wiping the whole file would lose it.
    """
    state = load()
    changed = False
    for key in ("refresh_token", "logged_in_at"):
        if state.pop(key, None) is not None:
            changed = True
    if changed:
        _write(state)


def refresh_token() -> str | None:
    token = load().get("refresh_token")
    return str(token) if token else None


def email() -> str | None:
    """The Benepass login email.

    Env override, then the account we last logged in as, then the address the
    last `benepass login` asked a code for — that last one so a retry before the
    first successful login does not have to be told the address again.
    """
    state = load()
    stored = state.get("email") or state.get("login_email")
    return os.environ.get("BENEPASS_EMAIL") or (str(stored) if stored else None)


def account_mismatch() -> tuple[str, str] | None:
    """(configured, token owner) when the resolved address is not the token's.

    `$BENEPASS_EMAIL` wins over the stored address, so exporting a second
    account's address without logging in as it leaves the two disagreeing —
    and the token, the workspace and every answer still belong to the stored
    one. Silent when nothing disagrees.
    """
    owner = load().get("email")
    configured = email()
    if not owner or not configured or str(owner) == configured:
        return None
    return configured, str(owner)


# -- the pending login challenge -------------------------------------------


def set_pending(
    challenge_session: str, challenge_name: str, address: str, requested_at: int
) -> None:
    """Remember the challenge a `login` started, so `login --code` can finish it."""
    save(
        **{
            PENDING_KEY: {
                "Session": challenge_session,
                "ChallengeName": challenge_name,
                "email": address,
                "requested_at": requested_at,
            }
        }
    )


def pending() -> dict[str, Any] | None:
    value = load().get(PENDING_KEY)
    return value if isinstance(value, dict) else None


def clear_pending() -> None:
    state = load()
    if state.pop(PENDING_KEY, None) is not None:
        _write(state)
