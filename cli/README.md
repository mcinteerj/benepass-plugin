# benepass — CLI reference

CLI for the **Benepass** employee benefits platform: check category balances, see what is about to expire, inspect what each benefit accepts, list transactions, and submit out-of-pocket expenses for reimbursement.

This is the mechanics reference. The [plugin README](../README.md) covers installation and the safety framing; the `benepass` skill covers *which* category to claim against and how to turn a receipt into a claim, and the three user-invoked skills (`/benepass:setup`, `/benepass:backfill`, `/benepass:sweep`) drive this CLI from those rules.

`benepass --help` lists the commands and `benepass <cmd> --help` is authoritative for flags; this table is the map.

| Command | What it answers | Flags beyond `--json` |
|---|---|---|
| `whoami`, `workspaces` | who this session is, and which workspace it carries | — |
| `profile` | the whole account in one call: employer, country, local currency, and every benefit's caps, windows and schedule | — |
| `benefits` | enrolled benefits + available balance | `-v` (benefit ids, per-expense caps — each labelled with its own currency) |
| `balances` | the same accounts with full account detail | — |
| `expiring` | what is about to be lost, and when | `--days N` (**default 150** — raise it to see further out), `--all` (contributions too) |
| `changes` | what moved since anyone last looked | `--full`, `--reset` |
| `categories` | what each benefit accepts, narrowest-first | `--benefit <id>`, `-e`, `--markdown` |
| `merchants` | which categories a merchant satisfies | `--benefit <id>` |
| `requirements <benefit>` | what a benefit demands as proof | `--json` carries the raw validations, including the eligible date window |
| `transactions` | card rows and claims | `--since`, `--until`, `--type`, `--search`, `--benefit`, `--limit`, `--offset`, `-P` |
| `show <id>` | one record in full (always JSON) | — |
| `match` | is this purchase already on file? the double-dip check | `--amount` (required), `--currency` (advisory flag, not a filter), `--date`, `--window`, `--days` |
| `options <txn>` | which other benefits could have paid for a charge | — |
| `reclassify <txn>` | move a card charge to another benefit | `--benefit`, `--confirm` |
| `tasks` | Benepass's own "action required" task feed — **often empty while a claim is genuinely bounced; not the bounce check** | — |
| `submit`, `delete <id>`, `upload <file>` | file, withdraw, attach | `--confirm` on the two writes |
| `currencies` | currency codes and their Benepass ids | — |
| `api <path>` | any endpoint with the stored session | `-X`, `--body`, `--confirm` |
| `login` | start or finish a login | `--email`, `--code`, `--force` |
| `logout` | forget the session and the change snapshot | — |

`--json` gives the raw payload — parse that rather than the text tables. It is on every read command in the table **plus `submit`**, whose preview and filed claim both carry it, which is why the third column lists only the flags *beyond* it. The eight commands that do **not** take it: `show`, `upload` and `api` (already JSON), `reclassify`, `delete` and `tasks` (preview/confirm text), and `login`/`logout`. Passing it to one of those is a usage error and exits 2, not a broken CLI.

## Install and run

The only prerequisite is [uv](https://docs.astral.sh/uv/). The `benepass` wrapper in this directory bootstraps itself on first run: it creates a venv at `~/.cache/benepass-cli/venv` (override with `BENEPASS_HOME`), installs this package into it, and records a hash of the sources so an updated copy reinstalls automatically. Nothing is ever written inside this directory — when this ships as a Claude Code plugin, the plugin directory is replaced on every update.

The CLI runs with Python's safe path (`-P`) and bytecode writing off, so the directory you happen to be standing in is not on `sys.path`: a `benepass/` folder or a stray `httpx.py` in your project cannot shadow the installed copy of a tool that holds a session token, and running the wrapper from inside `cli/src` cannot drop a `__pycache__` into the plugin directory.

```
./cli/benepass --help              # from a checkout
uv tool install ./cli              # or install `benepass` onto PATH, from a clone
uv tool install git+https://github.com/mcinteerj/benepass-plugin#subdirectory=cli
uv tool install git+ssh://git@github.com/mcinteerj/benepass-plugin#subdirectory=cli   # SSH clone
```

While the repository is private, both `git+` forms need your own git credentials — see [the plugin README](../README.md#install-details). With an SSH-only setup the `https` form fails with a bare `Repository not found`.

The wrapper is bash and assumes a POSIX virtualenv layout, so it covers macOS, Linux and WSL. On native Windows use the standalone install above and call plain `benepass`. The wrapper needs one of `sha256sum`, `shasum` or `openssl` to fingerprint its sources — every supported platform ships at least one.

## Logging in

Benepass has no passwords. Signing in means answering a 6-digit code emailed to the account address, so login is always two halves: ask for a code, then supply it.

```
benepass login --email you@example.com    # first time (or set BENEPASS_EMAIL)
benepass login --code 123456              # finish it
benepass whoami
```

`benepass login` picks whichever ending it can:

| Situation | What happens |
|---|---|
| `BENEPASS_OTP_COMMAND` is set | The command is polled every 5s for up to 120s — a ceiling on the whole call, including a poll still running when it arrives — and the first code it prints completes the login. Nothing to type. |
| stdin is a terminal | It prompts `Login code: ` inline. |
| Anything else (an agent, a pipe, a cron job) | It prints where the code was sent and exits **0**. Read the code from the mailbox, then run `benepass login --code <code>`. |

The pending challenge is stored between those two commands, because they are two separate processes. **Cognito expires a challenge in about three minutes**, so a code supplied more than five minutes later is refused with a message telling you to start again — rather than being sent and coming back as a misleading "incorrect username or password".

**A Cognito challenge Session is single-use, including when the answer is wrong.** Cognito either re-issues the challenge with a fresh Session or kills the attempt outright; the CLI stores the replacement when there is one, so retrying `login --code` with the right code works, and clears it and exits **3** ("run `benepass login` for a fresh code") when there is not. Re-sending a spent Session is what used to fail a correct code and blame the code.

**Only a 4xx counts as a refusal.** A 5xx from Cognito, or a network that cannot be reached at all, says nothing about the code you typed: those report the transport failure, exit 1, and leave the pending challenge in place so the same code can be sent again. Reporting them as "that code was refused" would blame a correct code and throw away a live challenge.

**The one-time code is safe in argv.** It is single use and dies in minutes. The refresh token it buys is neither, which is why that is never accepted as an argument and never printed.

Any command re-logs in automatically when the stored session has expired — but only if it *can* get a code by itself (`BENEPASS_OTP_COMMAND`, or a terminal). Otherwise it fails with **exit code 3**, reserved for "you need to log in", and a message naming the two commands to run. Every other API or runtime failure exits 1; an argument-parsing error (unknown command, unknown flag, missing required option) exits 2.

`benepass login` is a repair tool, not a step: no workflow should call it routinely. `--force` re-logs in even when a session already exists.

**The refresh token lasts roughly a day** (measured: a session was still good 23 hours later). Two consequences: expect a login-code email about once per day of use, which is normal rather than a sign of trouble; and every machine keeps its own session, so each one mints its own. A burst of commands does not re-login — the access token is cached in-process.

## Where the credential lives

The refresh token and the resolved workspace id are written to `~/.config/benepass/session.json`, mode `0600` inside a `0700` directory, per machine. The file is *created* at that mode — via an `os.open` with the mode, not a write-then-chmod, which would leave the credential world-readable for the instant in between — and replaced by rename, so a crash or a full disk cannot leave a truncated file that silently reads as "no session". The short-lived *access* token is cached in memory only, so each invocation pays one refresh→access exchange (~150 ms) and a second credential never touches disk.

It is deliberately:

- **not in any shared secret store and not in git** — it is re-mintable in seconds, so per-machine state is a smaller blast radius than a synced secret;
- **never a CLI argument and never printed** — an agent session keeps a lasting copy of every command it runs, so a token in argv or stdout would be a plaintext copy that only a revocation undoes. This is why the tool holds the token itself instead of taking it per call the way the upstream MCP does.

`benepass logout` clears it, along with the local change snapshot (`cache.json`, which holds that account's balances, merchant names and transaction ids). It deliberately **keeps the account address**, so the next `benepass login` does not have to be told it again — the credential is what a logout is for, not the account name. A fresh login as a *different* address clears everything, snapshot included, so one account's data is never diffed against another's.

There is no `--workspace` flag: the workspace id is resolved once, on the first command after a login, and stored. If Benepass ever rejects it ("requires a workspace"), `benepass workspaces` shows what the account has and `benepass logout` followed by a fresh login re-resolves it.

`$BENEPASS_EMAIL` outranks both the stored address and `--email`, which matters when they disagree: every command other than `login` refuses with exit 3 rather than answering from the account that owns the stored token, and `login` names both addresses. Against an exported `$BENEPASS_EMAIL`, a *contradicting* `--email` is refused outright rather than logged in — it would spend a one-time code on a session no later command would use.

## Common use

```
benepass profile                        # the whole account: employer, currency, every benefit
benepass benefits                       # benefits + available balance
benepass benefits -v                    # + benefit ids and per-expense caps
benepass expiring                       # what you are about to lose
benepass transactions --limit 50
benepass show expense_abc123            # full record, JSON
benepass upload ~/receipt.pdf           # upload a receipt, print its presigned_url
benepass requirements benefit_abc123    # note? receipt? what qualifies?
benepass match --amount 42.50 --date 2026-08-27   # already claimed, or already on the card?
benepass currencies --json | jq '.[] | select(.code=="USD")'
```

## The account at a glance

`benepass profile` answers everything about the account that a setup conversation would otherwise have to ask for, in one command: the login address, the employer and workspace, the country on the employment record, the currency Benepass renders money in, and for every benefit its id, balance in both currencies, eligible-category count, the purchase dates it accepts, its refresh cadence, its rollover cap and its next expiry.

```
benepass profile
benepass profile --json     # the same, with the per-benefit detail a script wants
```

It reads about two dozen endpoints and takes twenty-odd seconds, so it is a once-per-setup command rather than something to poll.

Three fields deserve their own warning, because each is a way to be confidently wrong:

- **Local currency is derived, not read.** Benepass never states it — it only renders into it. Every balance carries both the USD ledger cents and the same money rendered locally, so the ratio of the two is the rate Benepass used, and the currency list publishes that rate per currency. One candidate matches within a tenth of a percent. The output says which of `derived` (one candidate), `ambiguous` (several, and it lists their codes), `no_match` (a rate was readable but no published currency is within a tenth of a percent of it — a rounded rendering of a small balance does this) or `unknown` (every balance is zero, so there was no ratio to take) it is; treat anything but `derived` as "ask the user".
  A rendering like `KD153.750` is read **both** ways before it gives up — the five currencies with three minor digits (KWD, BHD, OMR, JOD, TND) put three digits after the decimal point, which is exactly the shape every other locale writes a thousands mark in, and the thousands reading derives a rate 1000× the published one that matches nothing. The second reading is offered only to currencies publishing three minor digits, so it can rescue those five without inventing a match for anything else.
- **Refresh cadence is derived too**, from the gaps between scheduled contributions over the next 400 days. An annual pot has only one contribution in that horizon, which is reported as `once in 400d` rather than guessed at, because an interval needs two points.
- **The address on file is not the user's address.** `/v2/me/` carries a `legal_address`, and on a multinational employer it is the *employer's* registered address — a real account reads a head-office city on one continent for an employee living on another. `profile` deliberately does not report it, and prints `City, timezone: not exposed by the API` instead. **No `/v2/me*` endpoint exposes a timezone at all.** Ask for both.

Date of birth, phone number and that address are also left out for a second reason: no decision needs them, and every command an agent runs is archived permanently.

`CLAIMS FROM` in the benefits table is `date_gte`, the oldest purchase date the benefit will accept. It is a live bound that can move — see the warning under [Submitting a reimbursement](#submitting-a-reimbursement) — so re-read it rather than quoting an old answer.

**`profile` reports the next expiry as a date and nothing else.** The expiration event carries Benepass's own at-risk amount, computed from the balance as it stands today with none of the contributions scheduled before that date applied — the figure that reads `$0.00` forever for a pot quietly forfeiting its whole monthly top-up. `benepass expiring` walks the schedule and reports PROJECTED, and that is the only exposure figure to quote; `profile --json` deliberately does not carry the other one.

**A `-` in that column is an answer, not a gap:** the benefit's substantiation policy states no `date_gte`, so it accepts any purchase date and `claim_window.from` is `null` in `--json`. Anything deriving a backfill floor from this column must ignore `-` rows rather than treat them as the earliest date — and where *every* benefit shows `-`, there is no stated floor at all.


## What each benefit accepts

The app's **Policy overview** screen, from three per-benefit endpoints:

```
/v2/me/benefits/{id}/eligibility-categories/   what you may buy
/v2/me/benefits/{id}/allowed-merchants/        buy ANYTHING here
/v2/me/benefits/{id}/disallowed-merchants/     buy nothing here
```

```
benepass categories                 # benefits narrowest-first, + merchant overrides
benepass categories -e              # + Benepass's spec text and example purchases
benepass categories --markdown      # the generated policy reference
benepass merchants --benefit <id>   # the general merchant catalog — NOT an eligibility source
```

Narrowest-first ordering is the point: category count is the objective narrowness signal, and the claiming rule is to use the narrowest benefit that legitimately covers a purchase.

Keep the markdown rendering somewhere the agent can read it, and regenerate it when it goes stale — it is your employer's policy, not part of this tool:

```
benepass categories --markdown > ~/.config/benepass/categories.md
```

> **Don't derive categories from `/v2/me/merchants/`.** An earlier version of this tool unioned the `eligibility_categories` of each benefit's merchants, which under-reports by roughly half — only categories with at least one curated merchant survive, so whole eligible categories vanish. The policy endpoints above are authoritative.

## Finding things

`transactions` takes server-side filters: `--since` / `--until` (YYYY-MM-DD), `--type` (`card`, `reimbursement`, `employer_contribution`, `expiration` — one only), `--search` (merchant substring), `--benefit`, `--limit`, `--offset`.

The PURCHASED column (on by default) is the date the claim was filed *for* — the receipt's date — as opposed to DATE, which is when the row was created. Benepass stores that date in two shapes, `MM/DD/YYYY` from this CLI and an ISO timestamp from its own web app, and both are read. On a reimbursement filed months late the two differ wildly: two claims filed the same day can be for different billing months, and only PURCHASED tells them apart. It costs one extra API call per reimbursement row, because the list endpoint carries no claim data; `--no-purchase-dates` / `-P` skips those fetches when speed matters. Card rows reuse their transaction time.

```
benepass transactions --type expiration          # everything that has already expired
benepass transactions --search Uber --since 2026-08-01
benepass options ictxn_xxx                       # which other benefits could pay for this
```

> **Benepass silently ignores a query parameter it does not recognise and still returns 200.** `transaction_type`, `date_from`, `ordering` and `page_size` all come back with the full unfiltered set. Prove any new filter by the row count changing, never by the request succeeding.

## Is this purchase already on file?

```
benepass match --amount 42.50 --date 2026-08-27
benepass match --amount 48.20 --currency GBP --date 2026-08-27 --window 1
benepass match --amount 76.99 --json
```

`match` is the anti-double-dip check as a command: run it before filing a receipt. A **card** row at the same amount and date means the benefit card already paid for that purchase, so filing a reimbursement claims the same money twice; a **reimbursement** row means this receipt has been filed before. It searches both.

**It matches on amount, not merchant name,** because the card network's descriptor rarely resembles the merchant's own brand — a transit tap arrives as a transport authority's code, a rideshare as a support URL — and a name search misses exactly the rows that matter. The amount compared is the **merchant** rendering, the figure printed on the receipt, never the USD ledger figure or Benepass's conversion into the local currency.

**`--currency` is advisory and excludes nothing.** It flags the rows whose merchant currency is not the code you passed — `ccy≠` in the FLAGS column, `"currency_mismatch": true` in the JSON — and leaves every one of them in the answer. The reason is that Benepass's own label is not reliable: a card row has been seen carrying **a currency the purchase was not made in**, and while the flag was a filter that row was invisible to a `match` passing the receipt's own code, so the double-dip guard answered "No matches" for a charge sitting in the list. A false "no" here is a claim filed twice; a flagged extra row costs a second of reading. Two currencies sharing a symbol is still the case worth telling apart, and the flag does that without hiding anything.

| Flag | What it does |
|---|---|
| `--amount` | Required. The receipt's own figure, positive. Matched to within half a cent. |
| `--currency` | The receipt's ISO code (`GBP`, `IDR`, …). **Advisory:** rows in another currency, and rows whose currency Benepass never stated (CCY `LOCAL`), are flagged `ccy≠` rather than dropped. The footer counts them and says why. |
| `--date` | Purchase date, `YYYY-MM-DD` only. Without it, amount alone is matched over the scan range. |
| `--window` | Days either side of `--date` to accept. Default **3**. |
| `--days` | How far back to scan when `--date` is absent. Default **400**. |

**`--date` accepts ISO and nothing else.** `submit` tolerates an unambiguous slash date because a human checks its preview; here a misread date would answer "no matches" for a purchase that *is* on file, and the whole value of this command is that its "no" can be trusted.

Three things about the dates it compares:

- **The `DATE` column is the purchase date, not the filing date.** For a card row those are the same moment; for a reimbursement the purchase date lives in the claim, and a backfilled receipt can be filed months later. A `~` prefix means that claim carried no purchase date at all, so the filing date is shown instead, and a `-` means the row stated no readable date either way. **Both are listed whatever `--date` says**, and `--window` is not applied to them: their purchase date is unknown, and unknown is not proof this is a different purchase — the same reason a row with no stated merchant currency survives `--currency`. Judging them on their filing date would drop exactly the late-filed duplicate this command exists to catch.
- **Give it slack.** The Benepass web app stores a purchase date as the user's local midnight converted to UTC, so a purchase made on the 30th in UTC+13 is stored as `…-29T13:00:00Z` and reads a day early. The default `--window 3` absorbs that; `--window 0` will miss it.
- **Reimbursements are scanned with no upper bound; card rows have one.** `--since`/`--until` filter the *filing* time, and a claim filed long after the purchase is precisely the late-filed duplicate worth catching, so the reimbursement pass is bounded below only. A card row's filing time *is* its purchase time, so with `--date` the card pass is bounded above at `--date + --window + 1 day` — the extra day absorbs the timezone shift — which is what stops an old date on a busy account paging through years of history.

An empty result exits **0** and prints the range it searched — "no matches" is an answer, not a failure.

**An unfinished scan is not a "no".** Two things leave one unfinished, and both set `truncated` in the JSON and print `SCAN INCOMPLETE` on **stdout as well as stderr**: paging hitting the 2000-transaction runaway guard, and the server handing back a page carrying rows it has already given us — the offset-ignoring or offset-capping habit of an unofficial API. That second one ends the scan, because twenty copies of page one help nobody, but it says nothing about what lies past where the server stopped. An empty result after either means *unknown*, not *nothing on file*.

What ends a scan cleanly is `total_count` being reached, or an empty page — the server saying there is nothing at this offset. Never a page merely arriving shorter than the 100 it asked for, which on an API that silently caps whatever it likes would turn an unfinished scan into a confident "no". `total_count` is read only as a reason to stop, never as grounds for calling an empty page incomplete: it is the count the server chose to report for a filtered query, and treating a larger number as "rows remain" would mark every dedup scan unusable if it were ever computed over unfiltered rows.

Checking one by hand afterwards takes **two** queries, because the two row types date differently: `transactions --type card --since <date−window> --until <date+window+1>` for the card side (`--until` is exclusive, and the extra day absorbs the timezone shift), and `transactions --type reimbursement --since <purchase date>` with **no** `--until` for the claim side, reading the PURCHASED column. An upper bound on the second is what hides a late-filed duplicate — the whole reason this command's own claim pass is unbounded above.

`--json` emits an envelope — `{"truncated": bool, "matches": [...]}`, never a bare list, because the caller filing a claim off this answer has to be able to tell "nothing matched" from "the scan never finished". Each match is `{type, id, merchant, amount, formatted_amount, currency, currency_mismatch, date, date_is_filing_date, benefit, account_id, status}`.


## Noticing what changed

An agent running `benepass benefits` has no way to know a transaction landed or the employer edited an eligibility list. So the CLI keeps a local snapshot of balances, transaction ids and category names at `~/.config/benepass/cache.json` (mode 0600 — no receipts, no tokens, nothing leaves the machine).

```
benepass changes           # balances, new transactions, status moves
benepass changes --full    # also re-reads the eligibility catalog (slower)
benepass changes --reset   # re-baseline without reporting
```

**`changes` is the only command that writes the cache.** The three commands that already fetch the relevant data — `benefits`, `transactions` and `expiring` — read it and print a one-line stderr nudge when something is pending, so it keeps nagging until someone looks. `transactions` does that only for an unfiltered first page — the snapshot holds the newest 100 rows unfiltered, so a row an older `--since`, a `--search` or an `--offset` pulled up is one `changes` will never show, and warning about it could not be cleared. The others stay silent rather than pay for an extra API call. Set `BENEPASS_NO_CHECK=1` to silence the nudge.

## Fixing what Benepass already decided

Benepass auto-classifies every **card** swipe to a best-fit benefit, and its best fit is frequently the broadest one. `reclassify` moves a charge to a narrower benefit that also covers it — the priority rule applied after the fact, freeing the pot that has to absorb miscellaneous spend later.

```
benepass reclassify ictxn_xxx --benefit benefit_yyy     # previews
benepass reclassify ictxn_xxx --benefit benefit_yyy --confirm
benepass tasks                                          # Benepass's task feed — see the warning below
```

> **`tasks` is not how you find a bounced claim.** It renders Benepass's own task feed, which has been observed empty for a solid week while a claim sat in `action_required` waiting for a better receipt. Treat an empty `tasks` as no information at all. **The check that works is the claim list itself:**
>
> ```
> benepass transactions --type reimbursement
> ```
>
> and read the status column: anything that is not `complete` — `action_required` above all — is a claim wanting something from the user. `benepass show <expense_id>` then gives `.claim.manual_review_reasons`, which names the defect to repair.

Card charges only — reimbursements are refused with a clear message. Benepass rejects a benefit that isn't eligible for that transaction, so a wrong guess is refused rather than silently mis-filed.

`delete` is the other direction of undo: it withdraws a **reimbursement** claim that was mis-filed (a duplicate of a card charge, the wrong purchase) while the claim is still pending. Finalized claims have no undo and are refused. It verifies the claim is really gone afterwards, because Benepass's DELETE endpoint silently no-ops when the path lacks its trailing slash.

```
benepass delete expense_xxx             # previews
benepass delete expense_xxx --confirm
```

## Projected vs reported expiry

`expiring` prints two exposure columns. **AT RISK NOW** is Benepass's own event amount, computed from the balance as it stands today — it never applies contributions scheduled before the expiry date, so a pot currently under its rollover cap reports `$0.00` indefinitely. **PROJECTED** walks the schedule, applies each contribution, and recomputes, assuming no spending.

They diverge exactly where it matters: a monthly contribution against a cap it will exceed reads as zero risk right up until the money dies. **Read PROJECTED.**

A `~` figure is the tool's own estimate — available minus the rollover cap — used where Benepass reported no amount for the event. It is exact for pots that roll over nothing.

## Currency — read this before quoting a number

Benepass holds balances in **USD** and displays them in the employee's local currency at the day's rate. The API exposes both: `amount` is USD cents, `formatted_local_amount` is the converted string. Contributions and expirations are recorded in USD with **no** conversion.

So a bare `$` figure is ambiguous, and mixing the two silently compares different currencies. `benefits -v` and `expiring` therefore print `AVAIL USD` and `AVAIL LOCAL` as separate columns — don't compare across them. The tool assumes no particular local currency; it prints whatever the row says it is.

**The per-expense cap has the same problem, per row.** Benepass states it as `local_max_expense_amount` on some enrollments and `max_expense_amount` (USD) on others, and one account can carry each. `benefits -v` therefore prints the cap with its own label in the MAX/EXPENSE column (`150 LOCAL`, `200 USD`) and `profile --json` carries `max_per_expense_ccy` beside `max_per_expense`. Compare a receipt against a cap only in the currency the row names.

**A transaction has three renderings, not two**, and on any purchase not made in the local currency they are three different numbers:

| Field | What it is |
|---|---|
| `formatted_merchant_amount` + `merchant_currency` | What the receipt says — the currency the purchase actually happened in |
| `amount` | USD cents; the ledger figure taken off the pot |
| `formatted_local_amount` | Benepass's conversion into the employee's local currency, carrying no label of its own |

**`merchant_amount` is in minor units, and the scale is read off the row rather than assumed.** Every live row seen so far has been a two-minor-digit currency, where the figure is hundredths — a rupiah charge arrives as `-98765400` for the `-Rp987.654,00` its own rendering prints. That says nothing about a currency Benepass records in major units (JPY, KRW) or with three minor digits (KWD, BHD, OMR, JOD, TND), where a flat ÷100 would be 100× or 10× out. So `match` scales by whichever of ÷100, ÷1 or ÷1000 reproduces the row's own `formatted_merchant_amount`, falling back to ÷100 where there is no rendering to check against; `submit` scales by the `decimals` **Benepass's own currency list publishes** for the code you passed, which is the list its app works from and the right answer by construction (see § Submitting a reimbursement).

**Read those decimals off `benepass currencies`, not off a local convention or ISO 4217** — the three disagree. Benepass publishes IDR with `decimals: 2` and JPY with `decimals: 0`, so a rupiah claim is sent in hundredths even though Indonesian prices are quoted in whole rupiah; a dry run of IDR 92,900 accordingly previews `IDR 92,900.00`, and a claim filed that way went through. The preview always prints the figure that will be sent, which is the check to make.

Every command that prints a transaction shows the **merchant** rendering with its currency beside it (the `CCY` column in `transactions`; a `currency` field in the JSON from `options`, `reclassify` and `delete`), falling back to the USD ledger for contributions and expirations, which have no merchant. Reaching for `formatted_local_amount` yourself is how a claim filed in one currency gets reported as its conversion into another while still wearing the first currency's code — a real number, in the wrong currency, matching neither the receipt nor the pot.

## Submitting a reimbursement

Submission **previews by default** and only goes through with `--confirm`:

```
benepass requirements <benefit_id>       # what this benefit needs as proof
benepass submit --benefit benefit_abc123 --merchant "Example Store" \
  --amount 42.50 --currency USD --date 2026-08-20 --note "Standing desk mat" \
  --receipt ~/receipts/desk-mat.pdf
# ^ dry run: prints what would be sent, plus any policy warnings

benepass submit ... --confirm            # actually submits
```

**`--currency` is the code printed on the receipt, and it defaults to `USD`.** Nothing validates it against the receipt, so an unset flag on a £48.20 or €63.00 purchase files that number as USD — a different amount, under a figure that looks right. The dry run prints the code it would send (`DRY RUN — would submit GBP 48.20`); check that token, not only the digits. `benepass currencies` lists the codes.

**The amount is sent in that currency's minor units**, scaled by the `decimals` Benepass publishes for it (2 where it publishes none) — the same list its own app uses, so this is Benepass's answer rather than a guess. It does not always match ISO 4217 or local habit: `benepass currencies` gives IDR 2 and JPY 0, so an IDR 92,900 receipt previews as `IDR 92,900.00` and is sent as 9,290,000 minor units, and that is the shape a filed rupiah claim went through as. Where the published figure is not 2 — a currency recorded in major units, or one of the five with three minor digits — the preview adds a warning naming the exact minor-unit figure it will send, because the amount line reads identically at any scale and it is the one error the human gate cannot otherwise catch. **The preview's figure is the check**: read it before approving. `--json` carries `minor_digits` and `merchant_amount` for the same reason. Note also that `--amount` is capped at 100,000 as a typo guard, which is a low ceiling for a currency whose everyday prices run in thousands.

**`--date` is required by every benefit** and is the field most likely to go wrong. Prefer `YYYY-MM-DD`. A slash date is accepted only when it cannot be read two ways: `03/09/2026` is refused outright, because Benepass reads it as 9 March while much of the world means 3 September, and the preview would echo the ambiguous string back unchanged. The preview prints the parsed date in words (`25 August 2026`) so it can actually be checked.

The dry run also warns when the benefit requires a `--note` or `--receipt` you have not supplied, and when the purchase date falls outside the benefit's eligible window (`date_gte` / `date_lte`). These are warnings, not gates: some `required` flags are conditional — a benefit may demand a receipt only above a threshold — so blocking on them would refuse legitimate claims.

`date_gte` is a **lower bound that can move**: it is read live from the benefit's own validations, and where an employer has configured a rolling window a backlog of old receipts stops being claimable all at once, at whatever boundary that window turns on. So "old purchases are usually still fine" is not the same as "this backlog is safe" — read the bound before saying so, with `benepass requirements <benefit_id> --json` or a dated preview.

**Assume there is no undo.** A new claim lands `pending` with `can_update: true`, and the amount moves from `available` into `held`, so there IS briefly a window — but approval timing is unpredictable (measured: 0.27s, 0.28s, 21 minutes, and one still pending long after), and once approved it is `finalized`, `can_update: false`, with no withdraw path. It is **not** a property of the benefit: what gets held back looks like a random review sample — two identical claims filed together went one each way, and the oldest pending one tends to clear first — so there is no "this benefit approves instantly" to rely on. The pre-submission preview is the only control you can rely on.

## Environment variables

| Variable | Purpose |
|---|---|
| `BENEPASS_EMAIL` | Benepass login email. Saves passing `--email`. |
| `BENEPASS_OTP_COMMAND` | Shell command that prints the emailed login code — makes login unattended. See below. |
| `BENEPASS_HOME` | Where the bootstrap venv lives (default `~/.cache/benepass-cli`). |
| `BENEPASS_STATE_DIR` | Override the session/cache directory (default `~/.config/benepass`). It is created — or narrowed — to mode 0700, so point it at a directory of its own rather than at `~/.config` or anything shared. **Give it an absolute path.** Exported but empty counts as unset and falls back to the default, matching the `${BENEPASS_STATE_DIR:-$HOME/.config/benepass}` the skills' shell blocks use; a relative path is refused outright, since it would mean a different session per working directory, and so is one starting with `~` — the skills' `${BENEPASS_STATE_DIR:-…}` blocks leave a tilde unexpanded, so expanding it here would put your session in `$HOME` and your preferences in a directory literally named `~`; the error names the absolute path to use instead. |
| `BENEPASS_NO_CHECK` | Set to any value to silence the "N changes pending" stderr nudge. |

### BENEPASS_OTP_COMMAND

The contract, so any mail tool can satisfy it:

- It is run **through the shell**, every 5 seconds, for up to 120 seconds. That is a ceiling on the whole poll, not on the last attempt's start: a command still running when the budget expires is cut short, so `benepass login` returns inside the window a caller can budget for.
- Two variables are exported to it: `BENEPASS_OTP_SINCE` (epoch seconds, one second BEFORE the code was requested — a mail-side date filter is second-granularity and may be exclusive, so the window starts a hair early rather than excluding the code that arrived) and `BENEPASS_EMAIL`.
- The code is read out of its stdout: a **six-digit run next to the word "code"** wins, then any six-digit run, and only if there is neither does a 4–8 digit run count. A longer run is never sliced up, so an order id or an epoch is harmless. A **four-digit year is not** — a `Date:` header or a copyright footer would be taken as the code if six digits were merely the first thing tried, which is why they are not. Printing the code and little else is still the safe shape: pipe through `grep -oE '[0-9]{6}' | head -1` if your mail command is chatty.
- A **non-zero exit means "not yet"** — the poller keeps going, and that command's output is ignored entirely.

A worked example, with `your-mail-cli` standing in for whatever mail client you have (a CLI, an MCP shim, a script):

```bash
export BENEPASS_OTP_COMMAND='your-mail-cli search \
  --from donotreply@getbenepass.com \
  --subject "Your Benepass Login Code" \
  --newer-than "$BENEPASS_OTP_SINCE" \
  --newest --plain'
```

Four things to get right in that command:

1. **Return the newest matching message.** Mail clients thread consecutive login-code emails, and a thread-oriented `get` hands back the thread's *first* message — i.e. the previous code, which fails with an error that blames the code rather than the search.
2. **Match the six digits, not the sentence around them.** A regex keyed to the email's wording — `code is (\d{6})`, `Your login code: (\d{6})` — breaks silently the day the provider rewrites the template, and the symptom reads as anything but that: `benepass login` sits out the full 120-second poll and gives up, while every requested code is sitting unread in the mailbox. `grep -oE '[0-9]{6}' | head -1` over a message body that has been narrowed by sender and date survives a reword; a wording-keyed pattern does not.
3. **Filter on `BENEPASS_OTP_SINCE`**, or exit non-zero until a new message arrives. Otherwise the first poll finds a code from an earlier login and confidently submits a stale one.
4. **Keep mail credentials out of the string.** This one ends up in more places than an ordinary export: agent sessions archive every command they run, the setup skill records the line verbatim in `PREFERENCES.md`, and a scheduled run needs its own copy in the crontab or the launchd plist. A password or token written inline is then a plaintext copy in all three that only a rotation undoes — the same reason the refresh token is never an argument here. If your mail command needs one, put it in a small wrapper script that reads it from a keyring or an environment variable it sources itself, and set `BENEPASS_OTP_COMMAND` to the script's path.

## Escape hatch

`benepass api /v2/me/accounts/` calls any API path with the stored session — useful for endpoints the CLI doesn't wrap yet.

**A non-GET call through it writes to the real account** — `-X POST /v2/me/expenses/` files a claim exactly as `submit` does — so it previews by default and needs `--confirm`, the same gate. The path is validated: it must start with a single `/`, because a path like `@example.invalid/v2/me/` would resolve to a different host and take the `Authorization` header with it.

```
benepass api /v2/me/accounts/                                            # runs
benepass api /v2/me/claims/<claim_id>/ -X PATCH --body '{...}'           # previews
benepass api /v2/me/claims/<claim_id>/ -X PATCH --body '{...}' --confirm # sends
```

`benepass upload <file>` is the other half of repairing a bounced claim: it uploads a receipt and prints the `presigned_url` to put in the claim's `substantiation_items`. It files nothing on its own.

## Endpoints that do not exist

Checked, so nobody repeats the hunt: there is no `/v2/me/benefits/` collection and no benefit-detail route (both 404), no eligibility-category collection outside the per-benefit path, and nothing more direct for expiry than the `next-events/` + `schedules/max-rollover-amount/` pair already used. `/v2/me/accounts/{id}/transactions|balances|summary/` exist but add nothing — `summary` is zero on every perk account.

Nor is there anywhere to read the things `profile` has to derive or leave blank. `/v2/me/settings/`, `/v2/me/preferences/`, `/v2/me/profile/`, `/v2/me/employments/`, `/v2/me/organization/`, `/v2/me/employer/`, `/v2/me/countries/`, `/v2/me/config/` and `/v2/me/features/` are all 404. `/v2/me/cards/` exists and carries issuer, status and design, but no currency. **No endpoint anywhere returns a timezone, a local-currency code, or an address that is the employee's own** — `/v2/me/` has `country` (the employment's) and `legal_address` (the employer's), and that is the lot.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — including "a login code was emailed, now run `login --code`" |
| 1 | Any API or runtime error: request failed, receipt missing, refused write |
| 2 | Usage error — unknown command, unknown flag, missing required option. Raised by the argument parser, before any API call; the message on stderr names the flag |
| 3 | Login required, and this process could not obtain a code by itself |

## Development

```
export PYTHONDONTWRITEBYTECODE=1   # keeps .pyc out of the plugin directory
uv run --no-project --with ruff ruff check .
uv run --no-project --with ruff ruff format --check .
uv run --no-project --with mypy --with pytest --with typer --with httpx mypy
uv run --no-project --with pytest --with typer --with httpx pytest
```

The tests are offline by design: the Cognito calls are stubbed, and the currency fixtures are invented rows shaped like live payloads. Nothing in the suite touches a real account.

## Notes

Benepass publishes no public API. The endpoints here are the ones its own `employee-web` app uses, mapped independently by the MIT-licensed [domdomegg/benepass-mcp](https://github.com/domdomegg/benepass-mcp) (Adam Jones), which this tool's API layer is modelled on. **Unofficial and unsupported — it can break without warning if Benepass changes their API.**
