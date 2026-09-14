"""State files hold a live credential, so how they are written is part of the design.

Two properties, both of which the obvious `write_text` + `chmod` breaks: the file
is never observable at any mode but 0600 (not even for the instant between
creation and the chmod), and a write either lands whole or not at all.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from benepass import session


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_file_is_created_at_0600_not_narrowed_to_it(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create-then-chmod would expose the token at the umask's mode first."""
    seen_before_chmod: list[int] = []
    real_chmod = Path.chmod

    def spy(self: Path, mode: int, **kwargs: Any) -> None:
        if self == session.STATE_FILE and self.exists():
            seen_before_chmod.append(_mode(self))
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", spy)
    session.save(refresh_token="not-a-real-token")

    assert seen_before_chmod == []
    assert _mode(session.STATE_FILE) == 0o600
    assert _mode(state_dir) == 0o700


def test_an_existing_permissive_directory_is_narrowed(state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o755)
    session.save(refresh_token="not-a-real-token")
    assert _mode(state_dir) == 0o700


def test_a_rewrite_stays_0600(state_dir: Path) -> None:
    session.save(refresh_token="not-a-real-token")
    session.save(workspace_id="ws_example")
    assert _mode(session.STATE_FILE) == 0o600
    assert json.loads(session.STATE_FILE.read_text())["workspace_id"] == "ws_example"


def test_a_failed_write_leaves_the_previous_session_intact(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.save(refresh_token="not-a-real-token", email="user@example.com")
    before = session.STATE_FILE.read_text()

    def explode(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="disk full"):
        session.save(workspace_id="ws_example")

    # Truncating in place would have left a file that parses as "no session".
    assert session.STATE_FILE.read_text() == before
    assert session.refresh_token() == "not-a-real-token"
    assert not list(state_dir.glob("*.tmp"))


def test_a_planted_symlink_at_the_temp_path_is_not_followed(
    state_dir: Path, tmp_path: Path
) -> None:
    """The temp path is fixed and predictable, so it is a plantable target.

    Following it would write the refresh token through the link — outside the
    0700 directory, at whatever mode the target already had.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "attacker-target.json"
    (state_dir / "session.json.tmp").symlink_to(target)

    session.save(refresh_token="not-a-real-token")

    assert not target.exists()
    assert not session.STATE_FILE.is_symlink()
    assert _mode(session.STATE_FILE) == 0o600
    assert session.refresh_token() == "not-a-real-token"


def test_a_symlinked_state_file_is_refused(state_dir: Path, tmp_path: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.json"
    session.STATE_FILE.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        session.save(refresh_token="not-a-real-token")
    assert not target.exists()


# --------------------------------------------------------------------------
# Where the state directory ends up
# --------------------------------------------------------------------------


def test_an_empty_state_dir_variable_means_the_default_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`export BENEPASS_STATE_DIR=` is how a cron line references an unset one.

    Read literally it is `.`, which would write the refresh token into whatever
    directory the command ran from — and narrow that directory to 0700 — while
    the skills' own `${BENEPASS_STATE_DIR:-$HOME/.config/benepass}` blocks kept
    reading PREFERENCES.md from the default.
    """
    monkeypatch.setenv("BENEPASS_STATE_DIR", "  ")

    assert session.state_dir() == session.DEFAULT_STATE_DIR


def test_a_relative_state_dir_is_refused_rather_than_silently_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It would mean a different session per working directory."""
    monkeypatch.setenv("BENEPASS_STATE_DIR", "state")

    with pytest.raises(SystemExit):
        session.state_dir()


def test_a_tilde_state_dir_is_refused_because_the_skills_cannot_expand_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expanding it here would split the state directory in two.

    The skills read the variable as `${BENEPASS_STATE_DIR:-…}`, and parameter
    expansion leaves a leading `~` alone — so a CLI that expanded it would write
    session.json to `$HOME/benepass-state` while PREFERENCES.md and sweep.json
    went to a directory literally named `~` under the run's working directory.
    Refusing keeps both sides on one directory, and the message names the path
    to use instead.
    """
    monkeypatch.setenv("BENEPASS_STATE_DIR", "~/benepass-state")

    with pytest.raises(SystemExit):
        session.state_dir()

    assert str(Path.home() / "benepass-state") in capsys.readouterr().err


def test_an_absolute_state_dir_is_used_as_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BENEPASS_STATE_DIR", str(tmp_path / "elsewhere"))

    assert session.state_dir() == tmp_path / "elsewhere"
