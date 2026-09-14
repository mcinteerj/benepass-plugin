# Benepass reference

Detail the main skill links to. Load when you need it, not by default.

**Command mechanics are not here.** Flags, filters, environment variables, exit codes, the API escape hatch and the endpoints known to be dead all live in the CLI README — `../../cli/README.md` from this file, or the absolute path the main skill gave you. Read that rather than a second copy of it: a duplicate goes stale silently. This page is what an agent needs and the CLI reference does not carry: how to tell the two transaction shapes apart, and why expiry differs between benefits.

## Card vs reimbursement — the full field table

Both types live on `/v2/me/transactions/` and both carry a `claim` with substantiation items, so "has a claim" does **not** mean "was reimbursed".

| Field | Card (`ictxn_` settled, `icauth_` pending) | Reimbursement (`expense_`) |
|---|---|---|
| `transaction_type` | `card` | `reimbursement` |
| `domain_object_name` | `CardTransaction` | `Expense` |
| `card`, `card_authorization` | populated | `null` |
| `merchant`, `merchant_category`, `merchant_network_id`, `location` | populated by the card network — the MCC data Benepass auto-classifies on | `null`; the merchant name is whatever was typed at submission |
| `origin` | `stripe` | `null` |
| `payout_balance_key` | `null` | `emp_…/payroll/available` |
| `eligible_benefits` | populated — the other pots that could pay for it, i.e. `reclassify` targets | `{}` — a filed claim cannot be moved |
| `balance_transactions` | 1 entry: the benefit balance debited | 2 entries: benefit debited **and** payroll credited |
| `is_auto_approved` | `true` | `false` — even on claims approved in under a second |

**Every field above the `transaction_status` line is on the DETAIL record, not the list.** `/v2/me/transactions/` returns `transaction_type`, the merchant fields, the amounts and `transaction_status` and nothing further — no `claim`, no `claim_status`, no `eligible_benefits`, no `balance_transactions`, no `is_auto_approved`. Read them from `benepass show <id>` (one call per row; `is_auto_approved` is at `.claim.is_auto_approved`, the bounce reasons at `.claim.manual_review_reasons`). A table like this one filled in off a list page is filled in off fields that were never there.

Two consequences that are easy to get wrong:

- **Reimbursements are repaid through payroll.** `payout_balance_key` points at `…/payroll/available`, and `/v2/me/bank-accounts/` is empty. "When does the money arrive" is a payroll-cycle question, not an ACH one.
- **`is_auto_approved` does not mean what it says.** It is `true` on card transactions (the classifier matched a merchant) and `false` on reimbursements, including ones that reached `approved` instantly. Read `claim_status` — and read it from the detail record, since the list page carries neither field.
- **Which claims get reviewed is not a property of the benefit.** It looks like a random sample: two identical claims filed together went one each way, and where several sit pending the oldest tends to clear first. There is no per-benefit rule to quote a user.

## Reading a transactions table

The **AMOUNT** column is the receipt's own figure in the currency **CCY** names — the merchant rendering, not a conversion. So grepping the receipt's number finds the row, and quoting the figure back to the user means what they think it means. If an amount ever disagrees with the receipt, that is a bug in the tool, not a conversion to explain away.

The **PURCHASED** column is the date the claim was filed *for*; **DATE** is when the row was created. On a claim filed months late the two differ wildly, and only PURCHASED tells two billing months of the same recurring bill apart.

The filter flags, and the trap that Benepass silently ignores an unrecognised query parameter while still returning 200: the CLI README § Finding things.

## Why expiry patterns differ between benefits

Benepass offers employers a handful of standard contribution/expiry templates, and an employer can pick a different one per benefit — "monthly stipend, expires end of month", "annual stipend, expires 31 December", "one-off new-hire grant, no expiry", and so on. That is why one pot bleeds every month while another dies once a year: a configuration choice, not a quirk of any account. See Benepass's own [schedule options](https://support.getbenepass.com/en/articles/11856867-what-are-the-options-for-contribution-and-expiration-schedule).

Forfeited money returns to the employer's pooled balance. It is gone from the employee either way.

`benepass expiring` reads the `next-events/` and `schedules/max-rollover-amount/` endpoints and walks the schedule forward to produce PROJECTED; **AT RISK NOW** is Benepass's own event amount, computed from today's balance with no future contributions applied.

## Where eligibility data comes from

`benepass categories` renders three per-benefit endpoints — eligibility categories, allowed merchants, disallowed merchants — benefits narrowest-first, and `--markdown` emits the reference the skill reads from `~/.config/benepass/categories.md`. The endpoints, the flags, and why the merchant catalog is not a substitute for them: the CLI README § What each benefit accepts.

## Per-user files

| Path | What |
|---|---|
| `~/.config/benepass/session.json` | refresh token, resolved workspace id, pending login challenge (0600, in a 0700 directory) |
| `~/.config/benepass/cache.json` | change-detection snapshot (0600) |
| `~/.config/benepass/categories.md` | generated eligibility reference the skill reads |
| `~/.config/benepass/PREFERENCES.md` | the user's policy (`.bak` beside it is the copy `/benepass:setup` takes before rewriting) |
| `~/.config/benepass/sweep.json` | last-run pointer written by `/benepass:sweep` and `/benepass:backfill` (schema in `sweep-engine.md`) |

`$BENEPASS_STATE_DIR` moves that directory elsewhere: the CLI reads it for `session.json` and `cache.json`, and the skills write the other three beside them, so the whole set travels together. **It has to be an absolute path** — set but empty counts as unset and falls back to the default, exactly as the `:-` in the blocks below does, and a relative one is refused rather than resolved against whatever directory a command happened to run in, as is one starting with `~` (the `:-` blocks leave a tilde unexpanded, so the CLI refuses it and names the expanded path to use). Any command block writing one of them opens with `STATE="${BENEPASS_STATE_DIR:-$HOME/.config/benepass}"` rather than hardcoding the default. Nothing per-user is ever written inside the plugin directory — it is replaced on every update. The refresh token lasts roughly a day, is never a CLI argument and is never printed, so a login-code email about once per day of use is expected rather than a fault.

## One API trap worth knowing here

`/v2/me/expenses/<id>/` answers 405 to everything but DELETE — and its DELETE **silently no-ops when the path lacks its trailing slash**, returning `null` indistinguishably from success. Always verify a deletion with `benepass show <id>` expecting a 404. `benepass delete` does this for you.

The `api` escape hatch itself, its `--confirm` gate, and the endpoints checked and found not to exist: the CLI README § Escape hatch and § Endpoints that do not exist.

## Provenance

Benepass publishes no public API. The endpoints here are the ones its own `employee-web` app uses, mapped independently by the MIT-licensed [domdomegg/benepass-mcp](https://github.com/domdomegg/benepass-mcp) (Adam Jones), which this tool's API layer is modelled on. **Unofficial and unsupported — it can break without warning if Benepass changes their API.**
