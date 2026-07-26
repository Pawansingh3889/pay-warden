"""Read-only audit dashboard.

Serves one self-contained page over the same AuditStore the MCP server writes
to, so what a judge sees is the audit trail itself rather than a retelling of it.
Built on starlette + uvicorn, both already present via `mcp` — no new dependency.

Run: python -m pay_warden.dashboard   (then open http://127.0.0.1:8080)
"""

import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from pay_warden.audit import AuditStore
from pay_warden.models import Verdict
from pay_warden.policy import Policy

_PAGE = Path(__file__).with_name("dashboard.html")


def _store() -> AuditStore:
    return AuditStore(os.environ.get("PAY_WARDEN_DB", "pay_warden_audit.sqlite3"))


def _policy() -> Policy:
    return Policy.load(os.environ.get("PAY_WARDEN_POLICY", "policies/default.yaml"))


def _agent_budgets(store: AuditStore, policy: Policy) -> list[dict]:
    """Spend against each agent's daily budget.

    Amounts are summed the same way the policy engine sums them, so the bars
    show what the engine will actually enforce rather than a prettier number.
    """
    rows = []
    for name, limits in policy.agents.items():
        spent = store.spent_today(name)
        budget = limits.daily_budget
        rows.append(
            {
                "agent": name,
                "spent": str(spent),
                "budget": str(budget),
                "cap": str(limits.max_single_purchase),
                "used": float(spent / budget) if budget else 0.0,
                "in_window": store.purchases_in_window(name, policy.velocity_window_minutes),
                "velocity_max": policy.velocity_max,
            }
        )
    return sorted(rows, key=lambda r: -r["used"])


async def state(request) -> JSONResponse:
    store, policy = _store(), _policy()
    attempts = store.recent(limit=200)
    verdicts = Counter(a["verdict"] for a in attempts)

    # Volumes are kept per currency: the engine sums them together for budget
    # purposes, but presenting one blended total here would be a fiction.
    minted: Counter = Counter()
    for a in attempts:
        if a["session_id"]:
            minted[a["currency"]] += Decimal(a["total_amount"])

    return JSONResponse(
        {
            "summary": {
                "attempts": len(attempts),
                "allowed": verdicts.get(Verdict.ALLOWED.value, 0),
                "denied": verdicts.get(Verdict.DENIED.value, 0),
                "needs_approval": verdicts.get(Verdict.NEEDS_APPROVAL.value, 0),
                "sessions": sum(1 for a in attempts if a["session_id"]),
                "minted": {k: str(v) for k, v in sorted(minted.items())},
            },
            "rules": Counter(
                a["rule_id"] for a in attempts if a["verdict"] == Verdict.DENIED.value
            ).most_common(6),
            "agents": _agent_budgets(store, policy),
            "attempts": attempts,
        }
    )


async def index(request) -> HTMLResponse:
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


app = Starlette(routes=[Route("/", index), Route("/api/state", state)])


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("PAY_WARDEN_HOST", "127.0.0.1"),
        port=int(os.environ.get("PAY_WARDEN_PORT", "8080")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
