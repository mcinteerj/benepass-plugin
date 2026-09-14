#!/usr/bin/env bash
# install-test.sh — clean-HOME smoke test for the Benepass plugin.
#
# Proves three things a published plugin has to get right:
#   1. the CLI bootstraps itself from nothing but `uv` on PATH, and runs;
#   2. it fails fast and legibly when there is no session, instead of hanging;
#   3. nothing is written inside the plugin directory, and no employer- or
#      person-specific identifier has crept into any file.
#
# Runs the CLI with a throwaway HOME and a PATH of /usr/bin:/bin plus the
# directory holding uv, so anything the CLI relies on from the author's machine
# shows up as a failure here. Safe to run repeatedly; touches nothing outside
# its own temp directory.

set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES=0
WARNINGS=0

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
warn() { printf '  warn  %s\n' "$1"; WARNINGS=$((WARNINGS + 1)); }
head2() { printf '\n== %s\n' "$1"; }

# ---------------------------------------------------------------- environment

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  echo "install-test: uv is not on PATH — install it first: https://docs.astral.sh/uv/" >&2
  exit 1
fi
# Resolve symlinks the way the wrapper does, rather than with `readlink -f`,
# which is GNU-only on the older macOS this script claims to support.
resolve_dir() { # path -> the real directory holding it
  local src="$1" dir
  while [ -L "$src" ]; do
    dir="$(cd -P -- "$(dirname -- "$src")" && pwd)"
    src="$(readlink "$src")"
    case "$src" in /*) ;; *) src="$dir/$src" ;; esac
  done
  (cd -P -- "$(dirname -- "$src")" && pwd)
}
UV_DIR="$(resolve_dir "$UV_BIN")"

# `timeout` is GNU coreutils and a stock macOS has neither it nor gtimeout.
# Without this probe every CLI assertion below exited 127 and read as "the CLI
# is broken" on the one platform the harness could not run on.
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
TEST_HOME="$TMPROOT/home"
TEST_BENEPASS_HOME="$TMPROOT/benepass-home"
mkdir -p "$TEST_HOME" "$TEST_BENEPASS_HOME"

# The download cache is shared with the real user when it exists: it makes the
# run minutes faster and does not weaken the test, because the virtualenv, the
# config directory and the session are all still built from nothing.
UV_CACHE="${UV_CACHE_DIR:-$HOME/.cache/uv}"
[ -d "$UV_CACHE" ] || UV_CACHE="$TMPROOT/uv-cache"

echo "repo:      $REPO_DIR"
echo "uv:        $UV_BIN"
echo "temp HOME: $TEST_HOME"
[ -n "$TIMEOUT_BIN" ] || echo "note:      no timeout/gtimeout — a hung CLI will not be killed"

# Run a command as if on a stranger's machine: clean env, reduced PATH, no TTY.
run_cli() {
  env -i \
    HOME="$TEST_HOME" \
    PATH="/usr/bin:/bin:$UV_DIR" \
    TERM=dumb \
    LANG="${LANG:-C.UTF-8}" \
    UV_CACHE_DIR="$UV_CACHE" \
    BENEPASS_HOME="$TEST_BENEPASS_HOME" \
    NO_COLOR=1 \
    "$@" </dev/null
}

# Same, with a wall-clock limit where the host has one to give.
run_cli_timeout() { # seconds cmd...
  local secs="$1"
  shift
  if [ -n "$TIMEOUT_BIN" ]; then
    run_cli "$TIMEOUT_BIN" "$secs" "$@"
  else
    run_cli "$@"
  fi
}

snapshot_tree() {
  find "$REPO_DIR" -name .git -prune -o -print | LC_ALL=C sort
}

TREE_BEFORE="$TMPROOT/tree-before"
TREE_AFTER="$TMPROOT/tree-after"
GIT_BEFORE="$TMPROOT/git-before"
GIT_AFTER="$TMPROOT/git-after"
snapshot_tree >"$TREE_BEFORE"
git -C "$REPO_DIR" status --porcelain --ignored >"$GIT_BEFORE" 2>/dev/null || : >"$GIT_BEFORE"

# Paths the plugin must never contain: the wrapper builds its virtualenv under
# $BENEPASS_HOME because the plugin directory is replaced on every update.
STRAYS=(
  "$REPO_DIR/cli/.venv"
  "$REPO_DIR/.venv"
  "$REPO_DIR/cli/src/benepass/__pycache__"
  "$REPO_DIR/cli/src/benepass_cli.egg-info"
  # uv.lock: gitignored, so a slip cannot ship the ~90 KB artifact, and still
  # listed here — this guard tests for the FILE, not for its ignore status, and
  # an unused lockfile inside a plugin directory that is replaced on every
  # update is stale the moment it is written.
  "$REPO_DIR/cli/uv.lock"
  "$REPO_DIR/uv.lock"
)
: >"$TMPROOT/strays-before"
for stray in "${STRAYS[@]}"; do
  [ -e "$stray" ] && printf '%s\n' "$stray" >>"$TMPROOT/strays-before"
done
true

# ------------------------------------------------------------------ 1. --help

head2 "CLI bootstraps and runs (--help)"
HELP_OUT="$TMPROOT/help.out"
set +e
run_cli_timeout 300 "$REPO_DIR/cli/benepass" --help >"$HELP_OUT" 2>&1
HELP_RC=$?
set -e
if [ "$HELP_RC" -eq 0 ]; then
  pass "cli/benepass --help exited 0"
else
  fail "cli/benepass --help exited $HELP_RC (expected 0)"
  sed -n '1,40p' "$HELP_OUT" | sed 's/^/        | /'
fi
if grep -q "submit" "$HELP_OUT" 2>/dev/null; then
  pass "--help lists the commands"
else
  warn "--help output does not mention 'submit' — is the app wired up?"
fi

# ------------------------------------------------- 1b. no GNU-only hashers

head2 "Bootstraps without GNU coreutils (the macOS case)"
ALT_HASHER=""
for h in shasum openssl; do
  command -v "$h" >/dev/null 2>&1 && ALT_HASHER="$h" && break
done
if [ -z "$ALT_HASHER" ]; then
  warn "neither shasum nor openssl on this host — cannot test the no-sha256sum path"
else
  # A PATH with everything the wrapper needs EXCEPT sha256sum, which macOS does
  # not ship: hard-coding it there killed every invocation before any benepass
  # message could be printed.
  FAKE_BIN="$TMPROOT/nognu"
  mkdir -p "$FAKE_BIN"
  for tool in bash env cat cut awk mkdir printf readlink dirname uv "$ALT_HASHER"; do
    src="$(command -v "$tool" || true)"
    [ -n "$src" ] && ln -sf "$src" "$FAKE_BIN/$tool"
  done
  NOGNU_CMD=("$REPO_DIR/cli/benepass" --help)
  if [ -n "$TIMEOUT_BIN" ]; then
    ln -sf "$TIMEOUT_BIN" "$FAKE_BIN/timeout"
    NOGNU_CMD=("$FAKE_BIN/timeout" 300 "${NOGNU_CMD[@]}")
  fi
  NOGNU_OUT="$TMPROOT/nognu.out"
  set +e
  env -i HOME="$TEST_HOME" PATH="$FAKE_BIN" TERM=dumb LANG="${LANG:-C.UTF-8}" \
    UV_CACHE_DIR="$UV_CACHE" BENEPASS_HOME="$TEST_BENEPASS_HOME" NO_COLOR=1 \
    "${NOGNU_CMD[@]}" >"$NOGNU_OUT" 2>&1 </dev/null
  NOGNU_RC=$?
  set -e
  if [ "$NOGNU_RC" -eq 0 ]; then
    pass "cli/benepass --help works with no sha256sum on PATH (fell back to $ALT_HASHER)"
  else
    fail "cli/benepass --help exited $NOGNU_RC without sha256sum — GNU-only dependency"
    sed -n '1,20p' "$NOGNU_OUT" | sed 's/^/        | /'
  fi
fi

# --------------------------------------------------- 2. no session, no TTY

head2 "No session: fails fast with a login message"
BEN_OUT="$TMPROOT/benefits.out"
START=$(date +%s)
set +e
run_cli_timeout 60 "$REPO_DIR/cli/benepass" benefits >"$BEN_OUT" 2>&1
BEN_RC=$?
set -e
ELAPSED=$(( $(date +%s) - START ))

if [ "$BEN_RC" -eq 124 ]; then
  fail "benefits hung and was killed after 60s"
elif [ "$BEN_RC" -eq 0 ]; then
  fail "benefits exited 0 with no session (expected a login failure)"
else
  pass "benefits exited non-zero ($BEN_RC) in ${ELAPSED}s without hanging"
fi

if [ "$BEN_RC" -eq 3 ]; then
  pass "exit code 3 (reserved for 'login needed')"
else
  fail "exit code was $BEN_RC, not the reserved 3 for 'login needed'"
fi

if grep -qiE "login|BENEPASS_EMAIL|not logged in|no session" "$BEN_OUT"; then
  pass "message tells the caller to log in"
else
  fail "no login guidance in the output"
fi
sed -n '1,10p' "$BEN_OUT" | sed 's/^/        | /'

# ------------------------------------------------- 3. nothing written in-repo

head2 "The repository is untouched"
snapshot_tree >"$TREE_AFTER"
git -C "$REPO_DIR" status --porcelain --ignored >"$GIT_AFTER" 2>/dev/null || : >"$GIT_AFTER"

if diff -q "$TREE_BEFORE" "$TREE_AFTER" >/dev/null; then
  pass "no file created or removed under the repo"
else
  fail "the run changed the repo's file list:"
  diff "$TREE_BEFORE" "$TREE_AFTER" | sed 's/^/        | /'
fi

if diff -q "$GIT_BEFORE" "$GIT_AFTER" >/dev/null; then
  pass "git status (including ignored files) unchanged"
else
  fail "git status changed across the run:"
  diff "$GIT_BEFORE" "$GIT_AFTER" | sed 's/^/        | /'
fi

for stray in "${STRAYS[@]}"; do
  if [ -e "$stray" ]; then
    if grep -qxF "$stray" "$TMPROOT/strays-before"; then
      fail "build artifact left inside the plugin directory (pre-existing, delete it): ${stray#"$REPO_DIR"/}"
    else
      fail "this run wrote inside the plugin directory: ${stray#"$REPO_DIR"/}"
    fi
  fi
done

if [ -x "$TEST_BENEPASS_HOME/venv/bin/python" ]; then
  pass "virtualenv built under \$BENEPASS_HOME, outside the plugin"
else
  fail "no virtualenv at \$BENEPASS_HOME/venv — where did the wrapper put it?"
fi

# ------------------------------------------------------ 4. de-identification

head2 "De-identification"

# The list of names to scan for is NOT in this repository, and must never be.
# Spelling out the author's employer, hosts, tooling and spending in the guard
# would republish exactly what the scrub removed, in a file that ships with the
# plugin — a scrubbed tree plus a verbose checker is not a scrubbed tree.
#
# So the maintainer keeps the list in a private file outside the tree. Its
# format is a shell fragment defining any of:
#
#   DEID_WORDS='\bname\b|other-term|...'   extended-regex alternation, case-insensitive
#   DEID_SANITIZE='s#permitted text##g'    sed -E rules, one per line, applied
#                                          before a line is judged (the author's
#                                          own name and repo slug legitimately
#                                          appear in the manifests and LICENSE)
#   DEID_SOFT='\b1,?234\b|...'             reported as a warning, never fatal
#
# A contributor who does not have that file still gets the structural half of
# the check below: the object-id shape, the tree assertions and the manifests.
DEID_TERMS_FILE="${BENEPASS_DEID_TERMS:-$HOME/.config/benepass-deid.terms}"
FORBIDDEN_WORDS=""
EXTRA_SANITIZE=""
SOFT=""
if [ -f "$DEID_TERMS_FILE" ]; then
  # shellcheck source=/dev/null
  . "$DEID_TERMS_FILE"
  FORBIDDEN_WORDS="${DEID_WORDS:-}"
  EXTRA_SANITIZE="${DEID_SANITIZE:-}"
  SOFT="${DEID_SOFT:-}"
fi

# Real Benepass object ids are a prefix plus a random base62 token, so they
# always carry a digit or a capital. Case-sensitive, which is why this is a
# separate pattern: lowercase code identifiers such as `benefit_type` or
# `max_expense_amount` are not ids and must not trip the check.
FORBIDDEN_IDS='\b(ictxn|icauth|expense|benefit|crcy|txn)_([a-z0-9]*[A-Z][A-Za-z0-9]*|[a-zA-Z]*[0-9][A-Za-z0-9]*)\b'

# Obvious placeholder ids — repeated letters, abc123, all-digit fixtures — are
# removed from a line before it is judged. Anything name-shaped that is
# legitimately permitted comes from DEID_SANITIZE in the private file.
SANITIZE='
  s#\b(ictxn|icauth|expense|benefit|crcy|txn)_(abc123|abc|xyz789|xyz|a{3,}|x{3,}|y{3,}|z{3,}|n{3,}|N{3,}|[0-9]+|123|<[^>]*>)\b##g
'"$EXTRA_SANITIZE"

hits_forbidden() { # stdin -> exit 0 when the sanitized line still offends
  local text
  text="$(sed -E "$SANITIZE")"
  if [ -n "$FORBIDDEN_WORDS" ] && printf '%s\n' "$text" | grep -qiE "$FORBIDDEN_WORDS"; then
    return 0
  fi
  if printf '%s\n' "$text" | grep -qE "$FORBIDDEN_IDS"; then return 0; fi
  return 1
}

GREP_EXCLUDES=(
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__
  --exclude-dir=.mypy_cache --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache
)

HITS="$TMPROOT/ident-hits"
: >"$HITS"
while IFS= read -r line; do
  if printf '%s\n' "${line#*:*:}" | hits_forbidden; then
    printf '%s\n' "$line" >>"$HITS"
  fi
done < <(
  {
    if [ -n "$FORBIDDEN_WORDS" ]; then
      grep -rnIE --binary-files=without-match "${GREP_EXCLUDES[@]}" \
        -iE "$FORBIDDEN_WORDS" "$REPO_DIR" 2>/dev/null || true
    fi
    grep -rnIE --binary-files=without-match "${GREP_EXCLUDES[@]}" \
      -E "$FORBIDDEN_IDS" "$REPO_DIR" 2>/dev/null || true
  } | LC_ALL=C sort -u
)

if [ -z "$FORBIDDEN_WORDS" ]; then
  warn "no term list at $DEID_TERMS_FILE — scanned for object ids only (set BENEPASS_DEID_TERMS to point at yours)"
fi
if [ -s "$HITS" ]; then
  fail "$(wc -l <"$HITS" | tr -d ' ') line(s) carry an identifier that must not ship:"
  sed "s#^$REPO_DIR/##; s/^/        | /" "$HITS"
else
  pass "no employer- or person-specific identifier found"
fi

# Soft checks: figures that are legitimate in an example and wrong as a
# statement of fact about "the" account. Reported, never fatal.
if [ -n "$SOFT" ]; then
  SOFT_HITS="$TMPROOT/soft-hits"
  grep -rnIE --binary-files=without-match "${GREP_EXCLUDES[@]}" \
    -E "$SOFT" "$REPO_DIR" >"$SOFT_HITS" 2>/dev/null || true
  if [ -s "$SOFT_HITS" ]; then
    warn "amounts or currencies to check are illustrative, not one real account's:"
    sed "s#^$REPO_DIR/##; s/^/        | /" "$SOFT_HITS"
  fi
fi

# De-identifying the working tree does nothing for what is already committed:
# a clone gets every past revision, and GitHub keeps unreachable objects
# fetchable by SHA even after a force-push. So check the history too — and check
# all three things a clone carries, because a scrub that only rewrites file
# content leaves two of them standing.
head2 "De-identification of the git history"
if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  HIST_HITS="$TMPROOT/hist-hits"
  : >"$HIST_HITS"
  # 1. Diff bodies. Both producers, exactly as the working-tree check above.
  #    Filtering history through the word list alone left FORBIDDEN_IDS
  #    unapplied, so a real object id in a past revision passed clean — and it
  #    passed clean precisely after the scrub this check exists to verify,
  #    because the names it does match are the first thing a scrub removes.
  DIFF_LINES="$TMPROOT/history-diff"
  git -C "$REPO_DIR" log --all --no-color --format='' -p 2>/dev/null |
    grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' >"$DIFF_LINES" || true
  # 2. Commit messages. `--format=''` above suppresses them, so the diff scan
  #    can never see them — and they are where a private repository name and an
  #    internal file path survive a re-root that only rewrites file content.
  #    Authorship itself is exempt: the repository owner's name and their
  #    GitHub noreply address are public by construction, and the
  #    Co-Authored-By trailer is standard tooling attribution — so scan the
  #    message BODIES, minus that trailer.
  META_LINES="$TMPROOT/history-meta"
  git -C "$REPO_DIR" log --all --no-color --format='%B' 2>/dev/null |
    grep -viE '^Co-Authored-By:' >"$META_LINES" || : >"$META_LINES"
  while IFS= read -r line; do
    if printf '%s\n' "$line" | hits_forbidden; then
      printf '%s\n' "$line" >>"$HIST_HITS"
    fi
  done < <(
    {
      if [ -n "$FORBIDDEN_WORDS" ]; then
        grep -ihE "$FORBIDDEN_WORDS" "$DIFF_LINES" "$META_LINES" 2>/dev/null || true
      fi
      grep -hE "$FORBIDDEN_IDS" "$DIFF_LINES" "$META_LINES" 2>/dev/null || true
    } | LC_ALL=C sort -u
  )
  if [ -s "$HIST_HITS" ]; then
    # FATAL, unlike every other pass here, because publishing is the one
    # irreversible step: GitHub keeps force-pushed objects fetchable by SHA, so
    # a green run that a maintainer reads as "safe to share" cannot be taken
    # back. This was advisory once, and the effect was that the single
    # de-identification assertion still true was also the only one that could
    # not fail the run. BENEPASS_ALLOW_DIRTY_HISTORY exists so the pre-re-root
    # period can still get a green CLI run — it has to be typed, never inherited.
    HIST_MSG="$(wc -l <"$HIST_HITS" | tr -d ' ') committed line(s) carry an identifier the working tree no longer has."
    if [ -n "${BENEPASS_ALLOW_DIRTY_HISTORY:-}" ]; then
      warn "$HIST_MSG (BENEPASS_ALLOW_DIRTY_HISTORY is set, so this is not fatal — do NOT publish on this)"
    else
      fail "$HIST_MSG"
    fi
    printf '        | the fix is to re-root the history (orphan commit, force-push) AND\n'
    printf '        | recreate the remote repository — a force-push alone leaves the old\n'
    printf '        | objects fetchable by SHA. The re-root must also rewrite the commit\n'
    printf '        | MESSAGES (they name private repositories and internal paths).\n'
    printf '        | Must happen before the repo is shared.\n'
    sed -n '1,5p' "$HIST_HITS" | cut -c1-100 | sed 's/^/        | /'
  else
    pass "no identifier in any committed revision or commit message"
  fi
else
  warn "not a git repository — skipped the history check"
fi

# --------------------------------------------------------------- 5. manifests

head2 "Manifests"
# A bare macOS without the Xcode command-line tools has no python3 and no jq,
# and reporting "not valid JSON" because the parser is missing is worse than
# saying nothing — `claude plugin validate` below covers it either way.
JSON_CHECK=""
if command -v python3 >/dev/null 2>&1; then
  JSON_CHECK="python3"
elif command -v jq >/dev/null 2>&1; then
  JSON_CHECK="jq"
fi
for f in .claude-plugin/plugin.json .claude-plugin/marketplace.json; do
  if [ ! -f "$REPO_DIR/$f" ]; then
    fail "$f is missing"
    continue
  fi
  case "$JSON_CHECK" in
    python3)
      if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$REPO_DIR/$f" 2>/dev/null; then
        pass "$f is valid JSON"
      else
        fail "$f is not valid JSON"
      fi
      ;;
    jq)
      if jq -e . "$REPO_DIR/$f" >/dev/null 2>&1; then
        pass "$f is valid JSON"
      else
        fail "$f is not valid JSON"
      fi
      ;;
    *) warn "$f present; no python3 or jq to parse it with" ;;
  esac
done
if command -v claude >/dev/null 2>&1; then
  if claude plugin validate "$REPO_DIR" >"$TMPROOT/validate.out" 2>&1; then
    pass "claude plugin validate passed"
  else
    fail "claude plugin validate failed:"
    sed 's/^/        | /' "$TMPROOT/validate.out"
  fi
else
  warn "claude not on PATH — skipped 'claude plugin validate'"
fi

# ----------------------------------------------------------------- summary

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
  printf 'install-test: PASS (%d warning(s))\n' "$WARNINGS"
  exit 0
fi
printf 'install-test: FAIL — %d failed assertion(s), %d warning(s)\n' "$FAILURES" "$WARNINGS"
exit 1
