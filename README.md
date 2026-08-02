# pay-warden

**A policy firewall for AI-agent spending, built on [Prava](https://prava.space).**

Agents shouldn't hold the card. pay-warden is an MCP server that sits between any AI agent
and Prava's agentic payment rails: the agent *requests* a purchase, the policy engine
decides — budgets, merchant lists, velocity, approval thresholds — and only compliant
requests mint a Prava payment session. Every attempt, allowed or blocked, lands in an
audit trail. The user's passkey remains the final human gate on the Prava side.

```
┌──────────┐   request_purchase   ┌─────────────────────────┐   POST /v1/sessions   ┌────────┐
│ AI agent │ ───────────────────▶ │        pay-warden        │ ────────────────────▶ │ Prava  │
│ (any MCP │                      │  policy engine + audit   │                       │sandbox │
│  client) │ ◀─────────────────── │  allow / deny / escalate │ ◀──────────────────── │        │
└──────────┘  decision + pay URL  └─────────────────────────┘  session + payment URL └────────┘
```

The agent never sees a card, a token, or a secret key. It sees a decision.

## Why

Same thesis as [sql-steward](https://github.com/Pawansingh3889/sql-steward) and
[query-warden](https://github.com/Pawansingh3889/query-warden), applied to money:
don't hand the agent raw power — compile its intent through a policy layer you control.

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # policy engine + audit tests, no network needed

cp .env.example .env        # add your sk_test_ key from dashboard.prava.space
python -m pay_warden.server # start the MCP server (stdio)
```

## Demo

```bash
python scripts/demo.py            # live — allowed requests mint real Prava sessions
python scripts/demo.py --dry-run  # policy only, no network
```

Runs a good agent beside a rogue one through the real MCP tools: a compliant purchase
mints a session, a denied merchant and an over-cap request are stopped, a third identical
purchase is refused once the daily budget is spent, and a large request parks for human
approval before being released. The denials never reach Prava's API — only allowed
requests produce a `POST /v1/sessions`. Ends with the audit trail for all six attempts.

## Operator dashboard

```bash
python -m pay_warden.dashboard   # http://127.0.0.1:8080
```

Read-only, over the same audit store the MCP server writes to — opened `mode=ro`, so a
five-second poll cannot take a write lock on the engine's own database, and pointing it at
an older file reports what is missing instead of migrating it.

The page is built around one question: **what happens at the moment a limit bites.** That
is the number nobody in agentic payments has, and both failure modes are fatal — limits so
tight that a human becomes the bottleneck, or so loose that the control is theatre.

| Panel | What it answers |
|---|---|
| Held for a human | of purchases the threshold parked, how many a person later released, and how long they took |
| Where the threshold could sit | every candidate threshold replayed against the purchases that actually reached the gate |
| What stopped a purchase | which rule ended each denied request, and whether a block reshaped the purchase or cancelled it |
| Identities the policy never heard of | `unknown-agent` denials, and which of them later registered |
| Merchant readiness | grades from a [canibuy](../canibuy) registry, ranked by money agents tried to spend there |

Every metric is defined in `insights.py` — the SQL, and what it does *not* mean. Three
rules keep the money honest: sums are `Decimal` and never SQLite's float `SUM()`; rows in a
currency the policy cannot price are excluded and counted rather than blended; and
`total_amount` is never added across currencies, because no exchange rate is stored.

Two things the page refuses to say. It says **value authorised**, never GMV or revenue — a
minted session means a payment page existed, never that money moved. And release rate is a
**lower bound**, because there is no rejection state: a person who refused an escalation
leaves a row identical to one nobody has looked at. The last panel lists five more limits
of this kind, each with its fix.

### Something to look at

```bash
python scripts/simulate.py --days 30 --per-day 160
PAY_WARDEN_DB=pay_warden_simulated.sqlite3 PAY_WARDEN_POLICY=policies/simulated.yaml \
  PAY_WARDEN_CANIBUY_DB=../canibuy/canibuy.sqlite3 python -m pay_warden.dashboard
```

The simulator **drives the real policy engine** — every row is a `PurchaseRequest`
evaluated by `Policy.evaluate` against a spend context built at the simulated moment, so no
combination appears on screen that the engine could not produce. Only the clock and the
write are synthetic, and every row is stamped `source='simulated'`.

That label is carried three ways so it cannot be cropped out of a screenshot: on the row,
on each panel's own slice of the payload, and by the single helper that builds every panel
heading — so removing the chip removes the number it qualifies.

Register in Claude Code / any MCP client:

```json
{ "mcpServers": { "pay-warden": { "command": "python", "args": ["-m", "pay_warden.server"], "cwd": "<this repo>" } } }
```

## MCP tools

| Tool | What it does |
|---|---|
| `preview_purchase` | Dry-run a purchase against policy — no session minted |
| `request_purchase` | Policy check → if allowed, mint a Prava session, return the payment URL |
| `approve_purchase` | Human override: release a request that escalated to `needs_approval` |
| `get_audit_log` | Recent attempts with decisions and fired rules |

## Policy

Declarative YAML in `policies/default.yaml` — per-agent daily budgets and single-purchase
caps, merchant allow/deny lists (glob), velocity limits, currency whitelist, and an
amount threshold above which requests park as `needs_approval` for a human.

## Prava backends

- `PRAVA_MODE=api` (default) — direct `POST /v1/sessions` against the sandbox
  (`https://api.prava.space`) with your `sk_test_` secret key.
- `PRAVA_MODE=cli` — shells out to the linked [`prava` CLI](https://github.com/Prava-Payments/prava-skills)
  (`prava sessions create` / `poll`) for the agent-wallet flow.

## Status

Hackathon scaffold for the Agentic Commerce Hackathon (Devfolio × Prava).
Core policy engine and audit store are tested; the dashboard and demo scenarios are
built during the hackathon itself.
