"""Policy engine: evaluates a PurchaseRequest against declarative YAML rules.

Evaluation is fail-closed and ordered — the first rule that fires wins:
unknown agent, mismatched totals, currency, merchant deny/allow, single-purchase
cap, daily budget, velocity, human-approval threshold. Only a request that
survives every rule is ALLOWED.
"""

from dataclasses import dataclass
from decimal import Decimal
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

import yaml

from pay_warden.models import Decision, PurchaseRequest, Verdict


@dataclass
class AgentLimits:
    daily_budget: Decimal
    max_single_purchase: Decimal


@dataclass
class SpendContext:
    """What the audit store knows about the agent's recent allowed spending."""

    spent_today: Decimal
    purchases_in_window: int


class Policy:
    def __init__(self, raw: dict) -> None:
        if raw.get("version") != 1:
            raise ValueError(f"Unsupported policy version: {raw.get('version')!r}")
        self.currencies: list[str] = raw.get("currencies", [])
        self.agents: dict[str, AgentLimits] = {
            name: AgentLimits(
                daily_budget=Decimal(cfg["daily_budget"]),
                max_single_purchase=Decimal(cfg["max_single_purchase"]),
            )
            for name, cfg in raw.get("agents", {}).items()
        }
        merchants = raw.get("merchants", {})
        self.merchant_allow: list[str] = merchants.get("allow") or []
        self.merchant_deny: list[str] = merchants.get("deny") or []
        velocity = raw.get("velocity", {})
        self.velocity_max: int = velocity.get("max_purchases", 0)
        self.velocity_window_minutes: int = velocity.get("window_minutes", 60)
        approval = raw.get("human_approval_over")
        self.human_approval_over: Decimal | None = (
            Decimal(approval) if approval is not None else None
        )

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def evaluate(self, req: PurchaseRequest, ctx: SpendContext) -> Decision:
        agent = self.agents.get(req.agent)
        if agent is None:
            return _deny("unknown-agent", f"Agent '{req.agent}' is not registered in policy")

        if req.products_total() != req.total_amount:
            return _deny(
                "total-mismatch",
                f"total_amount {req.total_amount} != sum of products {req.products_total()}",
            )

        if self.currencies and req.currency not in self.currencies:
            return _deny("currency", f"Currency {req.currency} not in {self.currencies}")

        host = urlparse(req.merchant_url).netloc.lower().removeprefix("www.")
        for pattern in self.merchant_deny:
            if fnmatch(host, pattern.lower()):
                return _deny("merchant-deny", f"Merchant '{host}' matches deny pattern '{pattern}'")
        if self.merchant_allow and not any(
            fnmatch(host, p.lower()) for p in self.merchant_allow
        ):
            return _deny("merchant-allow", f"Merchant '{host}' is not on the allow-list")

        if req.total_amount > agent.max_single_purchase:
            return _deny(
                "max-single-purchase",
                f"{req.total_amount} {req.currency} exceeds single-purchase cap "
                f"{agent.max_single_purchase} for '{req.agent}'",
            )

        if ctx.spent_today + req.total_amount > agent.daily_budget:
            return _deny(
                "daily-budget",
                f"Would take '{req.agent}' to {ctx.spent_today + req.total_amount} "
                f"of {agent.daily_budget} daily budget",
            )

        if self.velocity_max and ctx.purchases_in_window >= self.velocity_max:
            return _deny(
                "velocity",
                f"'{req.agent}' already made {ctx.purchases_in_window} purchases in the last "
                f"{self.velocity_window_minutes} min (max {self.velocity_max})",
            )

        if self.human_approval_over is not None and req.total_amount > self.human_approval_over:
            return Decision(
                verdict=Verdict.NEEDS_APPROVAL,
                rule_id="human-approval",
                reason=f"{req.total_amount} {req.currency} exceeds auto-approval threshold "
                f"{self.human_approval_over}; a human must release it",
            )

        return Decision(verdict=Verdict.ALLOWED, rule_id="pass", reason="All policy rules passed")


def _deny(rule_id: str, reason: str) -> Decision:
    return Decision(verdict=Verdict.DENIED, rule_id=rule_id, reason=reason)
