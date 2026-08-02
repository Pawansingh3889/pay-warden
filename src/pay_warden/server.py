"""pay-warden MCP server.

Exposes purchase-request tools to any MCP client. The policy engine and the
Prava credentials live here, server-side — the connected agent only ever
receives decisions and payment URLs.

Run: python -m pay_warden.server  (stdio transport)
"""

import json
import os
from typing import Callable, Optional

from mcp.server.fastmcp import FastMCP

from pay_warden import prava
from pay_warden.audit import AuditStore
from pay_warden.models import Decision, Product, PurchaseRequest, Verdict
from pay_warden.policy import Policy, SpendContext

mcp = FastMCP("pay-warden")

_policy = Policy.load(os.environ.get("PAY_WARDEN_POLICY", "policies/default.yaml"))
_audit = AuditStore(os.environ.get("PAY_WARDEN_DB", "pay_warden_audit.sqlite3"))

# Optional model guard consulted *after* the rules and only on requests the rules
# ALLOW — the one place rule-evasions can hide (an evasion is a deny-truth the
# rules waved through). It never sees a rule denial, so it cannot weaken one; it
# can only catch what the rules are structurally blind to. Left unset, behaviour
# is exactly the rules engine. A deployment injects one with `set_second_opinion`.
SecondOpinion = Callable[[PurchaseRequest, SpendContext, Decision], Decision]
_second_opinion: Optional[SecondOpinion] = None


def set_second_opinion(fn: Optional[SecondOpinion]) -> None:
    """Install (or clear) the model guard the engine consults on allowed requests."""
    global _second_opinion
    _second_opinion = fn


def _evaluate(req: PurchaseRequest):
    ctx = SpendContext(
        spent_today=_audit.spent_today(req.agent),
        purchases_in_window=_audit.purchases_in_window(
            req.agent, _policy.velocity_window_minutes
        ),
    )
    decision = _policy.evaluate(req, ctx)
    if decision.verdict is Verdict.ALLOWED and _second_opinion is not None:
        decision = _second_opinion(req, ctx, decision)
    return decision


def _build_request(
    agent: str,
    merchant_name: str,
    merchant_url: str,
    merchant_country: str,
    products: list[dict],
    total_amount: str,
    currency: str,
) -> PurchaseRequest:
    return PurchaseRequest(
        agent=agent,
        merchant_name=merchant_name,
        merchant_url=merchant_url,
        merchant_country=merchant_country,
        products=[Product(**p) for p in products],
        total_amount=total_amount,
        currency=currency,
    )


@mcp.tool()
def preview_purchase(
    agent: str,
    merchant_name: str,
    merchant_url: str,
    merchant_country: str,
    products: list[dict],
    total_amount: str,
    currency: str,
) -> dict:
    """Dry-run a purchase against policy. Nothing is minted or recorded."""
    req = _build_request(
        agent, merchant_name, merchant_url, merchant_country, products, total_amount, currency
    )
    return _evaluate(req).model_dump()


@mcp.tool()
def request_purchase(
    agent: str,
    merchant_name: str,
    merchant_url: str,
    merchant_country: str,
    products: list[dict],
    total_amount: str,
    currency: str,
) -> dict:
    """Request a purchase. If policy allows, a Prava payment session is minted
    and the payment URL returned for the user to complete with their passkey.
    Denied and needs-approval attempts are recorded in the audit trail."""
    req = _build_request(
        agent, merchant_name, merchant_url, merchant_country, products, total_amount, currency
    )
    decision = _evaluate(req)
    # Budgets are enforced in the policy's base currency, so that is what the
    # audit trail must store — not the amount as the agent expressed it.
    base_amount = _policy.to_base(req.total_amount, req.currency)

    if decision.verdict is not Verdict.ALLOWED:
        attempt_id = _audit.record(req, decision, base_amount=base_amount)
        return {"attempt_id": attempt_id, **decision.model_dump()}

    session = prava.create_session(req)
    attempt_id = _audit.record(
        req, decision, session["session_id"], session["payment_url"], base_amount=base_amount
    )
    return {
        "attempt_id": attempt_id,
        **decision.model_dump(),
        "session_id": session["session_id"],
        "payment_url": session["payment_url"],
    }


@mcp.tool()
def approve_purchase(attempt_id: str) -> dict:
    """Human override: release an attempt that parked as needs_approval.
    Mints the Prava session for the original request."""
    attempt = _audit.get(attempt_id)
    if attempt is None:
        return {"error": f"No attempt {attempt_id!r}"}
    if attempt["verdict"] != Verdict.NEEDS_APPROVAL.value:
        return {"error": f"Attempt {attempt_id} is '{attempt['verdict']}', not pending approval"}

    # Rebuild the original request from the attempt row. Rows written before
    # country/products were persisted fall back to a generic line item.
    products = json.loads(attempt["products"] or "[]")
    req = PurchaseRequest(
        agent=attempt["agent"],
        merchant_name=attempt["merchant_name"],
        merchant_url=attempt["merchant_url"],
        merchant_country=attempt["merchant_country"] or "US",
        products=products
        or [{"description": "approved purchase", "unit_price": attempt["total_amount"]}],
        total_amount=attempt["total_amount"],
        currency=attempt["currency"],
    )
    session = prava.create_session(req)
    _audit.mark_released(attempt_id, session["session_id"], session["payment_url"])
    return {"attempt_id": attempt_id, "released": True, **session}


@mcp.tool()
def reject_purchase(attempt_id: str, note: str = "") -> dict:
    """Human decision: refuse an attempt that parked as needs_approval.

    Mints nothing and calls Prava not at all. `note` is the person's own words
    and is stored separately from the policy engine's reason, which stays
    intact — a spender is owed the rule that fired *and* the answer they got,
    and neither should be paraphrased into the other.

    Refusing is deliberately its own verdict rather than leaving the row parked.
    Silence and a decision look identical in an audit trail, and a spender
    waiting on an answer nobody recorded is the worst outcome this whole flow
    can produce.
    """
    attempt = _audit.get(attempt_id)
    if attempt is None:
        return {"error": f"No attempt {attempt_id!r}"}
    if attempt["verdict"] != Verdict.NEEDS_APPROVAL.value:
        return {"error": f"Attempt {attempt_id} is '{attempt['verdict']}', not pending approval"}
    if not _audit.mark_rejected(attempt_id, note):
        # Lost a race with an approval between the read and the write.
        return {"error": f"Attempt {attempt_id} was decided by someone else first"}
    return {
        "attempt_id": attempt_id,
        "rejected": True,
        "note": note,
        "reason": attempt["reason"],
    }


@mcp.tool()
def get_audit_log(agent: str | None = None, limit: int = 20) -> list[dict]:
    """Recent purchase attempts (all agents, or one) with decisions and fired rules."""
    return _audit.recent(agent=agent, limit=limit)


if __name__ == "__main__":
    mcp.run()
