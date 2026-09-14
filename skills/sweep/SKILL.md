---
name: sweep
description: The repeatable pass — search the mailbox since the last sweep for receipts eligible for a Benepass benefit, drop anything the benefit card already paid, and file what the user approves. Ends with a compact summary suitable for a cron email.
disable-model-invocation: true
---

# Benepass sweep

The recurring catch of new claimable spend. For history — everything back to the edge of the eligible window — use `/benepass:backfill` instead; it runs the same engine over a long window.

## 1. Work out the window

```
STATE="${BENEPASS_STATE_DIR:-$HOME/.config/benepass}"
cat "$STATE/sweep.json"
```

`$STATE` is `$BENEPASS_STATE_DIR` where the user has set it and `~/.config/benepass` otherwise — the same directory `/benepass:setup` wrote to. Each command block runs in its own shell, so declare it in each one. **In a headless run, read the file with the `Read` tool at the absolute state path instead** — a Bash command carrying `${…:-…}` matches no `--allowedTools` prefix rule, so this block is denied there (engine § Per-user files).

```
WINDOW_END   = today
WINDOW_START = no file, unreadable, or an unrecognised "version" → today − LOOK_BACK
               last run had "reported_only": true → that run's window.start
               otherwise → date(last_run) − 3 days

LOOK_BACK    = PREFERENCES § Sweep "Look-back when there is no last-run pointer",
               35 days if that line is missing or unreadable
```

The look-back is the user's setting, not a constant: someone who edits it to 90 days after a gap has told you how far back to look, and quietly using 35 searches a third of what they asked for. Say which value you used and where it came from.

The three-day overlap is deliberate: a receipt often arrives days after the purchase, and `benepass match` drops anything already claimed, so overlapping costs a few API calls and closes the gap. Where `window.end` is earlier than `last_run`, take the earlier date as the basis so a bounded run leaves no hole.

State the window in one line before searching.

## 2. Run the engine

Follow `${CLAUDE_PLUGIN_ROOT}/skills/benepass/sweep-engine.md` from step 0 with that window, **the CLI at `${CLAUDE_PLUGIN_ROOT}/cli/benepass`** and **the base skill at `${CLAUDE_PLUGIN_ROOT}/skills/benepass/SKILL.md`** (the engine defers to it by section name and cannot resolve the path itself) — the engine writes its commands as plain `benepass` and is read off disk, so nothing in it will expand that path for you — and this hand-off: **attended** where you are in a session with the user, **unattended** in a headless `claude -p`, which reports and files nothing. Say which. The engine is the whole procedure — preconditions, what is at risk, the search plan, extraction, the `benepass match` dedup, eligibility, the numbered table, approval, filing, and the state file (`"mode": "sweep"`).

When you write that state file, **a `progress` block left by an unfinished `/benepass:backfill` is copied across untouched, and its `filed[]`/`skipped[]` entries are kept alongside your own** (engine step 9). Rewriting the file without them throws away a backfill's only resume pointer.

A sweep is short and should stay cheap: vendor queries from PREFERENCES § Vendors and search terms first, generic terms second, paginated until each is exhausted. If a sweep routinely returns nothing for a vendor row, say so — the row's search query probably needs fixing, and that is an edit to PREFERENCES rather than something to work around every month.

**Propose that edit; do not make it.** An **attended** session may apply it once the user has said yes to the new query. An **unattended** one never writes `PREFERENCES.md` at all — it puts the proposed change in its report and stops there. The file is the user's policy and the thing every later run trusts; a headless run has just read a mailbox anyone can write into, and nobody is there to check what the edit said. The permission scope in the cron recipe below covers the state directory, `PREFERENCES.md` included, so nothing mechanical prevents this — the procedure is the control.

## 3. Finish with the summary block

Whatever else you have said, end with this, plain text, no wider than about 70 characters — it is what lands in a cron email or a phone notification:

```
Benepass sweep — 2026-08-10 → 2026-09-14
At risk: Wellness USD 120.00 projected to expire 30 Sep
Candidates 4 · filed 2 · awaiting you 1 · duplicates dropped 2

Filed:
  Fibrenet Broadband  GBP 38.00  2026-09-02  Connectivity  pending
  Northgate Books     GBP 18.99  2026-09-06  Learning      pending
Awaiting you:
  Lightwell Yoga      GBP 60.00  2026-08-12  Wellness  (near window edge)
Next sweep covers purchases from 2026-09-11.
```

Where the run filed nothing because it could not ask, say so in that block — `filed 0 (report-only)` — and list what is waiting.

## Running it unattended

A headless run **reports and files nothing** (engine step 7), and it needs two things to be worth scheduling: mail access that works without a human, and somewhere for the report to land.

```
BENEPASS_STATE_DIR=/absolute/path/to/state    # only if the user has one set — check with `echo $BENEPASS_STATE_DIR`
BENEPASS_OTP_COMMAND=your-mail-cli search --from donotreply@getbenepass.com --newer-than "$BENEPASS_OTP_SINCE" --newest --plain | grep -oE "[0-9]{6}" | head -1
0 9 1 * *  cd /path/to/anywhere && /absolute/path/to/claude -p "/benepass:sweep" --allowedTools "Bash(/absolute/path/to/plugin/cli/benepass:*)" "Bash(your-mail-cli:*)" "Bash(jq:*)" "Bash(mkdir:*)" "Bash(chmod:*)" "Bash(cat:*)" "Bash(wc:*)" "Read(//absolute/path/to/state/**)" "Edit(//absolute/path/to/state/**)" | mail -s "Benepass sweep" you@example.com
```

Two honest caveats for that shape:

- **Login.** An unattended session cannot answer the emailed 6-digit code unless `BENEPASS_OTP_COMMAND` is set (CLI README § BENEPASS_OTP_COMMAND). **No mail password or token goes inside that line** — it is written into the crontab, into `PREFERENCES.md` and into this session's archived commands; point it at a wrapper script that fetches its own credential instead. Without it, a sweep that meets an expired session exits at engine step 0 with "login needed" and does nothing else. **The variable has to be in the crontab**, as above, or in a launchd plist's `EnvironmentVariables` — cron's shell reads no `~/.profile` or `~/.bashrc`, so an export that works when the user runs the sweep by hand is simply absent here. Same for `claude` itself: cron's `PATH` is `/usr/bin:/bin`, so give the absolute path from `command -v claude`. Both failures are late and quiet — a stored session keeps the job working until it expires.
- **Permissions, and why they are scoped.** Headless `claude -p` denies every tool call it has no rule for — leave the `--allowedTools` list off and the log holds one refusal and no report. The `Bash` rules above are what lets the run call the CLI, the mail CLI and the small utilities; the file rules are what it uses for `sweep.json` and `categories.md`, because Claude Code matches a `Bash` rule by command prefix and the skills' state blocks — `${BENEPASS_STATE_DIR:-…}`, a heredoc — match no rule and are refused however the list is written (engine § Per-user files). A scheduled run therefore reads and writes those files with the file tools, not with the shell blocks printed in this file.

  **Scope both rules to the state directory, and write the path out literally.** `"Read(//absolute/path/to/state/**)" "Edit(//absolute/path/to/state/**)"` — the `Edit(…)` rule is what gates the **Write** tool as well, verified under the default permission mode: inside the scope a write is allowed, outside it is denied. Bare `"Read" "Write"` would work too, and is the wrong trade: this run ingests a mailbox anyone on earth can post into, and an unscoped write rule turns a crafted receipt into a file-write anywhere the user can write. The path is a literal one because the rule is matched literally — `$BENEPASS_STATE_DIR` and `~` are not expanded inside it — so resolve it when you write the crontab, and that same resolved path is the one the run reads and writes at (engine § Per-user files).

  **`PREFERENCES.md` is inside that scope**, so the rules permit the run to rewrite the user's policy. The procedure is what forbids it: an unattended run proposes a preferences change and never applies one (§ 2 above, engine step 2).

  The CLI rule names the plugin's absolute path (`~/.claude/plugins/cache/benepass-plugin/benepass/<version>/cli/benepass`), which changes on every plugin update: re-check it after `/plugin update`. Never substitute `--dangerously-skip-permissions`.
- **The state directory.** `$BENEPASS_STATE_DIR` is read from the environment too, so a user who exports it in their shell profile and not in the crontab gets a scheduled run looking in `~/.config/benepass`, where there is no session and no `PREFERENCES.md`: exit 3 at engine step 0, monthly, into a log nobody reads. If they have one set, it needs its own crontab line as above (or `EnvironmentVariables` in the plist).
- **A Claude Code scheduled task whose prompt is this skill will not fire it** — `disable-model-invocation: true` blocks that path, deliberately, because a claim-filing procedure should start from a person. A cron or launchd entry running `claude -p "/benepass:sweep"` invokes it exactly as typing the command does.

Record whichever the user chose in PREFERENCES § Sweep.
