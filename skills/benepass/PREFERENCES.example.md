# Benepass preferences

Copy to `~/.config/benepass/PREFERENCES.md` and fill in — or run **`/benepass:setup`**, which interviews you and writes the whole file. This file is your policy, not Benepass's — the agent reads it at the start of every Benepass task and follows it over anything it would otherwise infer from the data. Keep the headings; replace the italic prompts with your answers, and delete the guidance comments as you go.

## Profile

<!--
Where you live and how you get about. `/benepass:setup` fills most of this from
`benepass profile` and asks you for the rest; it decides which merchants are
plausible for you and which benefits are live at all.

Nothing here is sent anywhere — it is local context so the agent stops guessing.
-->

*Country and city/region:* … *(`benepass profile` gives the country; the city it cannot — no /v2/me* endpoint exposes one)*

*Commute:* … *(car, train, bus, bike, walk, work from home — and whether you pay for it yourself)*

*Local currency, as Benepass renders it:* … *(from `benepass profile`; if you want figures quoted differently, say so under Display currency)*

*Timezone:* … *(also not exposed by the API — `/benepass:setup` asks for it alongside the city)*

## Mail access

<!--
Benepass logs in by emailing a 6-digit code, and sweeps look for receipts in your
mail, so what your sessions can read mail with decides what is possible.

Name the tool (an MCP server, a mail CLI, "none — I paste codes by hand"), the
mailbox it reads, and whether it runs headless. Headless means it works from cron
with nobody watching: an MCP connector generally does not, a mail CLI generally
does.

BENEPASS_OTP_COMMAND is the unattended-login hook — a shell command that prints
the code to stdout. Record it here if you set one up, and keep the export in your
shell profile, not in this file.

Keep passwords and tokens OUT of that command. The line is copied into this file,
into your crontab or launchd plist, and into the archived commands of every agent
session that runs it — a secret inline is a plaintext copy in all three that only
a rotation undoes. If your mail command needs one, point BENEPASS_OTP_COMMAND at a
small wrapper script that fetches the secret itself (a keyring, its own env file).

That export covers shells you start yourself and nothing else: cron and launchd
read neither ~/.profile nor ~/.bashrc, so a scheduled sweep needs its own copy —
a BENEPASS_OTP_COMMAND= line in the crontab, or EnvironmentVariables in the
plist. Without it the schedule works until the stored session expires, then fails
quietly with "login needed".
-->

*Mail tool:* …

*Mailbox address:* …

*Works headless:* …

*BENEPASS_OTP_COMMAND:* *(not set)*

## Benefit priority order

<!--
List your benefits best-first, by the name Benepass shows (`benepass benefits -v`).
When a purchase is eligible under more than one, the agent files it against the
highest one on this list.

Reasoning hint: narrowest pot first — the one accepting the fewest eligible
categories, or dying soonest — because a broad pot is the only one that can absorb
miscellaneous spend later. Override that when a big annual pot is badly underclaimed
and dies whole on a fixed date: an unclaimed annual grant loses more money than a
monthly pot forfeiting its excess.

Say which of these you are doing and why, so the agent can tell you when it would
have chosen differently rather than silently disagreeing.
-->

1. *Benefit name* — *why it ranks here*
2. *Benefit name*
3. *Benefit name*

## Corporate card, not the perk

<!--
Passing the eligibility check is not enough: many work expenses are eligible for a
benefit and should still go through your employer's expense system.

Name the system (corporate card, expense tool, "ask my manager", "we have none"),
and list what always goes there instead of Benepass. Common answers: laptops,
monitors, peripherals, dev tooling, LLM/API credits, the work phone bill, work
software, anything shipped to an office, anything booked through corporate travel.
-->

*Where work expenses go:* …

*Always goes there, never Benepass:* …

## Recurring repeats

<!--
A "straight repeat" is the same merchant, same kind of purchase, same benefit as a
claim you have already approved — a monthly internet invoice, say.

yes = the agent files a repeat with --confirm and tells you afterwards.
no  = every claim waits for your approval of that specific preview. (Default.)

Even with yes, a batch of candidates still comes to you as a list first, and anything
judgement-shaped drops back to asking: a changed amount beyond the bill's usual drift,
a new merchant, a different plausible benefit.
-->

*Auto-file straight repeats of an approved claim:* **no**

*Recurring bills this covers (merchant → benefit):* …

## Vendors and search terms

<!--
The table `/benepass:backfill` and `/benepass:sweep` work from: who you buy from,
which pot it belongs on, and how to find the receipt in your mail.

`/benepass:setup` proposes it from your own transaction history (past card
merchants and past reimbursements are the strongest signal for a repeat vendor),
from the recurring bills you named, and from likely merchants for your country and
your eligible categories. All of that is a proposal — edit it. A wrong row here
sends a sweep hunting for receipts that do not exist.

Mail search query: whatever your mail tool takes. `from:` / `subject:` for a
Gmail-style search; the CLI's own flags otherwise; the sender's address alone if
you search by hand.

That column takes exactly three shapes, and the last two are how you say "this
one is not searchable":

  1. A mail query — the normal case; the sweep runs it.
  2. `not a mail source — <where it comes from>`, e.g. "not a mail source —
     receipt comes by WhatsApp", "— paper receipt I photograph". The sweep
     skips the row and NAMES it in the report as not swept, so a clean run is
     never read as "nothing outstanding".
  3. `cross-check only — <why>`, e.g. "cross-check only — always pays on the
     benefit card". The row is here to be recognised, not searched; same
     treatment.

Anything else in the column is read as a query and run, so a note to yourself
written there becomes a search that finds nothing.

Typical amount is a sanity check, not a rule: a sweep compares a candidate against
it and flags one that has moved a long way from it — roughly a quarter either side —
with the old figure beside the new, rather than proposing it like any other row.
-->

| Merchant | Benefit | Mail search query | Recurring? | Typical amount |
|---|---|---|---|---|
| *Example ISP* | *Home internet* | *`from:billing@example-isp.com`* | *monthly* | *55.00 GBP* |
| … | … | … | *no* | … |

## How to reach me for approval

<!--
`--confirm` files a real claim against your employer, so the agent must have a way to
put the preview in front of you and get a yes on that specific claim — merchant,
amount and date. No blanket approvals, no consent inferred from silence.

Say which channels count, and what an approval looks like in each. If a session has
none of them available, the agent reports instead of filing.

The unattended line names where a REPORT should reach you — it is not a licence to
file. A scheduled run has nobody in it to approve a specific preview, so it reports
and stops whatever this section says; you file when you read the report.
-->

*Interactive session:* show me the preview and wait.

*Unattended / headless:* *(report only — where should it reach me?)* …

*What counts as approval:* …

## Display currency

<!--
Benepass holds balances in USD and shows a local conversion; transaction rows are in
the merchant's own currency. Three renderings, so every figure needs a label.

Name the currency you think in, so quoted figures land — e.g. "quote GBP, label USD
caps as USD". The tool never assumes one.
-->

*My local currency:* …

## Notes on claims

<!--
The note field is optional on most benefits and mandatory on some (`benepass
requirements <benefit_id>` says which).

A common policy: no note where the merchant name already says what it was; a short one
when the merchant is generic (a marketplace, a reseller) and the purchase is not
self-evident from the name.
-->

*Note policy:* …

## Sweep

<!--
How far back a routine sweep looks, and what you decided about running it on a
schedule.

The last-run pointer is NOT kept here — it lives in
`~/.config/benepass/sweep.json`, written by `/benepass:sweep` itself, so this file
stays something you edit by hand and nothing overwrites.

A scheduled, headless sweep REPORTS and never files: there is nobody there to
approve a claim. Filing happens when you next read the report.
-->

*Look-back when there is no last-run pointer:* **35 days** *(`/benepass:sweep` reads this line; edit it and the next unpointered sweep uses your number)*

*Schedule:* … *(cron/launchd running `claude -p "/benepass:sweep"`, a scheduled routine, or "I run it myself" — and how often)*

*Where a scheduled run should leave its report:* … *(a log file path, `| mail -s … you@example.com`, or a notification command — `/benepass:setup` asks for this when you pick a schedule)*

## Anything else

<!--
House rules the agent should know. Examples: a pot you have decided not to worry about
forfeiting and do not want raised again; a benefit you never want claimed against; a
purchase type you always want asked about; someone to route policy questions to.
-->

…
