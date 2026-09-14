"""Isolation every test module needs, in one place.

Both state paths are resolved at import time — ``session.STATE_DIR`` from
``$BENEPASS_STATE_DIR`` or ``$HOME``, and ``cache.CACHE_FILE`` from
``session.STATE_DIR`` — so rebinding one of them later does not move the other.
A module that patched only the session paths still deleted the *real* user's
``~/.config/benepass/cache.json`` the moment a test exercised the
account-switch branch, which silently re-baselines their change snapshot: the
next ``benepass changes`` reports nothing moved when something did.

So the patching lives here, autouse, and no test module owns a copy of it. The
directory deliberately does not exist yet, so the tests that assert it is
*created* at 0700 have something to create.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benepass import cache, session


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the session, the pending challenge and the cache at a temp dir."""
    target = tmp_path / "config" / "benepass"
    monkeypatch.setattr(session, "STATE_DIR", target)
    monkeypatch.setattr(session, "STATE_FILE", target / "session.json")
    monkeypatch.setattr(cache, "CACHE_FILE", target / "cache.json")
    monkeypatch.delenv("BENEPASS_STATE_DIR", raising=False)
    monkeypatch.delenv("BENEPASS_EMAIL", raising=False)
    monkeypatch.delenv("BENEPASS_OTP_COMMAND", raising=False)
    return target
