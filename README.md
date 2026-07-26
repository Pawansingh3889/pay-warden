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
