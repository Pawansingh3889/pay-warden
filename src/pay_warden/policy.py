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

        # Budgets and caps are denominated in one base currency. Without this,
        # amounts in different currencies were compared and summed as bare
        # numbers, so 100 INR counted the same as 100 USD against a budget.
        self.base_currency: str = raw.get("base_currency", "USD")
        self.rates: dict[str, Decimal] = {
            code: Decimal(str(rate)) for code, rate in (raw.get("rates") or {}).items()
        }
        self.rates.setdefault(self.base_currency, Decimal("1"))
        # A single-currency policy needs no rates; a multi-currency one that
        # omits them would silently under-count, so refuse to load it.
        missing = [c for c in self.currencies if c not in self.rates]
        if len(self.currencies) > 1 and missing:
            raise ValueError(
                f"Policy lists {len(self.currencies)} currencies but has no "
                f"{self.base_currency} rate for {missing}. Add a 'rates:' entry for each."
            )
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

    def to_base(self, amount: Decimal, currency: str) -> Decimal | None:
        """Convert to the policy's base currency, or None if no rate is known.

        None is a refusal, not a zero — callers must deny rather than let an
        unconvertible amount through unchecked.
        """
        rate = self.rates.get(currency)
        if rate is None:
            return None
        return (amount * rate).quantize(Decimal("0.01"))

    def _amount(self, amount: Decimal, currency: str, base: Decimal) -> str:
        """Render an amount, showing the base equivalent when it differs."""
        if currency == self.base_currency:
            return f"{amount} {currency}"
        return f"{amount} {currency} ({base} {self.base_currency})"

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

        base = self.to_base(req.total_amount, req.currency)
        if base is None:
            return _deny(
                "unknown-rate",
                f"No {self.base_currency} conversion rate for {req.currency}; "
                f"cannot check it against limits",
            )

        host = host_of(req.merchant_url)
        for pattern in self.merchant_deny:
            if fnmatch(host, pattern.lower()):
                return _deny("merchant-deny", f"Merchant '{host}' matches deny pattern '{pattern}'")
        if self.merchant_allow and not any(
            fnmatch(host, p.lower()) for p in self.merchant_allow
        ):
            return _deny("merchant-allow", f"Merchant '{host}' is not on the allow-list")

        if base > agent.max_single_purchase:
            return _deny(
                "max-single-purchase",
                f"{self._amount(req.total_amount, req.currency, base)} exceeds "
                f"single-purchase cap {agent.max_single_purchase} {self.base_currency} "
                f"for '{req.agent}'",
            )

        if ctx.spent_today + base > agent.daily_budget:
            return _deny(
                "daily-budget",
                f"Would take '{req.agent}' to {ctx.spent_today + base} of "
                f"{agent.daily_budget} {self.base_currency} daily budget",
            )

        if self.velocity_max and ctx.purchases_in_window >= self.velocity_max:
            return _deny(
                "velocity",
                f"'{req.agent}' already made {ctx.purchases_in_window} purchases in the last "
                f"{self.velocity_window_minutes} min (max {self.velocity_max})",
            )

        if self.human_approval_over is not None and base > self.human_approval_over:
            return Decision(
                verdict=Verdict.NEEDS_APPROVAL,
                rule_id="human-approval",
                reason=f"{self._amount(req.total_amount, req.currency, base)} exceeds "
                f"auto-approval threshold {self.human_approval_over} "
                f"{self.base_currency}; a human must release it",
            )

        return Decision(verdict=Verdict.ALLOWED, rule_id="pass", reason="All policy rules passed")


def host_of(url: str) -> str:
    """The registrable host a merchant rule matches against.

    Extracted so anything reporting on merchants joins on exactly the string the
    engine judged against. Two implementations of "which merchant is this" would
    eventually disagree, and the disagreement would be invisible — a coverage
    number quietly missing every `www.` host.
    """
    return urlparse(url).netloc.lower().removeprefix("www.")


def _deny(rule_id: str, reason: str) -> Decision:
    return Decision(verdict=Verdict.DENIED, rule_id=rule_id, reason=reason)
