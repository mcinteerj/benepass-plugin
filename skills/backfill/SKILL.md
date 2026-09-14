---
name: backfill
description: One-off historic catch-up — search the mailbox back to the edge of the Benepass eligible window for receipts that were never claimed, check each against the card rows, and file the ones the user approves. Run once after /benepass:setup, or whenever a backlog has built up.
disable-model-invocation: true
argument-hint: "[months back]"
---

# Benepass backfill

The one-time dig through history. `/benepass:sweep` is the same machinery on a short recent window — use that for the repeating pass.

Arguments: `$ARGUMENTS` — months back, as a number (`/benepass:backfill 18`). Absent or unparseable, use the account's own eligible window, below.

## 1. Work out the window, and say it out loud

**Is there a run to resume?** Read the state file before computing anything — an interrupted backfill leaves its place in it, and recomputing the full window pays for every mail search a second time:

```
STATE="${BENEPASS_STATE_DIR:-$HOME/.config/benepass}"
cat "$STATE/sweep.json"
```

A **`progress`** object (`covered_start`, `target_start`) means the last backfill stopped part-way. Resume it: `WINDOW_END` = `progress.covered_start`, `WINDOW_START` = `progress.target_start`, keep the existing `filed[]` and `skipped[]` and carry them into every later write, **keep the `window.end` already in the file** — this run's end date is months in the past, and writing it over the newest date the earlier blocks covered sends the next `/benepass:sweep` back over all of them (engine step 9's schema) — and say so in the one-line window statement — *"picking up where the last run stopped: 2025-03-14 → 2026-06-14 still to search, 3 already filed and 11 already declined."* A candidate matching a `skipped[]` entry has been decided already; re-propose a declined one only with the `previously declined` flag on its row. Then skip the window algebra below — it has been settled. The one exception is an `$ARGUMENTS` that asks for a window the checkpoint was not working towards: say both out loud, take the user's number as the new `target_start`, and keep the existing `filed[]`/`skipped[]` either way.

No `progress` key (or no file), work it out from the account:

```
${CLAUDE_PLUGIN_ROOT}/cli/benepass profile
```

`profile` prints each benefit's eligible date window in its **CLAIMS FROM** column (`claim_window.from` in `--json`; `date_gte` is the API's own name for the field). The **earliest of those across the benefits the user actually cares about** is the floor: nothing purchased before it can be claimed anywhere.

**A `-` in that column means the benefit states no lower bound at all** — it accepts any purchase date (`claim_window.from` is `null`). It is not a missing reading, and it must not be treated as one: a `-` row puts no floor under the search, so leave it out of the "earliest" calculation entirely.

```
WINDOW_END   = today
WINDOW_FLOOR = earliest CLAIMS FROM across the eligible benefits THAT STATE ONE
               (ignore `-` rows) — or none at all, if every one of them shows `-`
WINDOW_START = $ARGUMENTS given ? max(WINDOW_FLOOR, today − N months) : WINDOW_FLOOR
               no WINDOW_FLOOR and no $ARGUMENTS → today − 12 months, said out loud
               as a choice you made rather than a bound their account stated
```

Tell the user the window in one line before searching anything, with the reason: *"Your earliest eligible purchase date is 2025-10-01 (Learning; Wellness only reaches back to 2026-08-01), so I'll search 2025-10-01 → today."* Where no benefit states a bound, say that instead — *"none of your benefits states an earliest purchase date, so there is no floor to read; I'll search the last 12 months unless you want more"* — and never read a sentence with a literal `-` where a date should be. Where the benefits' floors differ a lot, name the tight ones — a candidate that clears the search window can still miss the pot you would file it against, and step 5 of the engine catches that per candidate.

**`date_gte` is a lower bound that moves.** On a rolling window a backlog stops being claimable all at once, so read it now rather than assuming old receipts are safe.

If `profile` exits 3, log in first — engine step 0.

## 2. Run the engine

Follow `${CLAUDE_PLUGIN_ROOT}/skills/benepass/sweep-engine.md` from step 0, with that window, **the CLI at `${CLAUDE_PLUGIN_ROOT}/cli/benepass`** and **the base skill at `${CLAUDE_PLUGIN_ROOT}/skills/benepass/SKILL.md`** (the engine defers to it by section name and cannot resolve the path itself) — the engine writes its commands as plain `benepass` and is read off disk, so nothing in it will expand that path for you — and this hand-off: **attended** where you are in a session with the user, **unattended** in a headless `claude -p`, which reports and files nothing. Say which. It is the whole procedure: preconditions, what is at risk, the search plan, extraction, the `benepass match` dedup, eligibility, the numbered table, approval, filing, and the state file.

Four things a long window changes:

- **Exhaust every query, then slice.** Engine step 2's rule holds twice over here: page each query until it is genuinely finished, because the mail tool's result cap — not the window's length — is what drops receipts silently, and expand every multi-message thread. Slicing a year into months is the second move, for a tool that cannot page: twelve slices per query, each small enough to come back whole.
- **Work it in blocks, newest first, and checkpoint each one.** Engine step 9 § Checkpoint a long run is the mechanism: about three months per block, `sweep.json` rewritten after each with the accumulated `filed[]`/`skipped[]` and a `progress` object. Without it an interrupted backfill — the likely outcome at this volume — leaves no trace, and the next attempt repeats every search and re-proposes everything the user already declined. Resuming from one is step 1's first move. Generic search terms go on the two newest blocks only; older blocks ride on the vendor table.
- **Expect volume, and stage it.** Past roughly 20 candidates, present the table one benefit at a time, best pot first, grouping near-identical rows by vendor as engine step 6 says, and get one approval per table rather than handing over a wall of rows. The per-benefit balance check in engine step 6 matters most here: a backlog routinely proposes more than a pot holds, and the excess is a "not now" list, not a batch of claims that will bounce.
- **Old receipts are normal.** Purchases well over a year old, including ones predating the benefit, are often still claimable. Preview rather than pre-rejecting — but honour the per-benefit `date_gte` the preview warns about.

## 3. Close the loop

Write `sweep.json` with `"mode": "backfill"` and no `progress` key (engine step 9) — dropping that key is what marks the run finished. It is also what the next `/benepass:sweep` reads to know where to start, so a backfill that ends today leaves the sweep a three-day overlap and nothing more to redo.

Finish by asking whether to set up the recurring pass, if PREFERENCES § Sweep has no schedule recorded: `/benepass:sweep` monthly by hand, or the cron shape in that skill.
