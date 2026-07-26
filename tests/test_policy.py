from decimal import Decimal

import pytest

from pay_warden.models import Product, PurchaseRequest, Verdict
from pay_warden.policy import Policy, SpendContext

POLICY = {
    "version": 1,
    "currencies": ["USD", "GBP"],
    "agents": {
        "shopper": {"daily_budget": "50.00", "max_single_purchase": "25.00"},
    },
    "merchants": {"allow": [], "deny": ["*.casino.example"]},
    "velocity": {"max_purchases": 3, "window_minutes": 60},
    "human_approval_over": "20.00",
}

NO_SPEND = SpendContext(spent_today=Decimal("0"), purchases_in_window=0)


def make_request(**overrides) -> PurchaseRequest:
    base = dict(
        agent="shopper",
        merchant_name="Blue Bottle Coffee",
        merchant_url="https://www.bluebottlecoffee.com",
        merchant_country="US",
        products=[Product(description="Latte", unit_price=Decimal("5.00"))],
        total_amount=Decimal("5.00"),
        currency="USD",
    )
    base.update(overrides)
    return PurchaseRequest(**base)


@pytest.fixture
def policy() -> Policy:
    return Policy(POLICY)


def test_clean_request_is_allowed(policy):
    decision = policy.evaluate(make_request(), NO_SPEND)
    assert decision.verdict is Verdict.ALLOWED


def test_unknown_agent_denied(policy):
    decision = policy.evaluate(make_request(agent="intruder"), NO_SPEND)
    assert decision.verdict is Verdict.DENIED
    assert decision.rule_id == "unknown-agent"


def test_total_mismatch_denied(policy):
    decision = policy.evaluate(make_request(total_amount=Decimal("9.99")), NO_SPEND)
    assert decision.rule_id == "total-mismatch"


def test_quantity_counts_toward_total(policy):
    req = make_request(
        products=[Product(description="Latte", unit_price=Decimal("5.00"), quantity=2)],
        total_amount=Decimal("10.00"),
    )
    assert policy.evaluate(req, NO_SPEND).verdict is Verdict.ALLOWED


def test_unlisted_currency_denied(policy):
    decision = policy.evaluate(
        make_request(currency="JPY", total_amount=Decimal("5.00")), NO_SPEND
    )
    assert decision.rule_id == "currency"


def test_denied_merchant_glob(policy):
    decision = policy.evaluate(
        make_request(merchant_url="https://lucky.casino.example/spin"), NO_SPEND
    )
    assert decision.rule_id == "merchant-deny"


def test_allowlist_when_present_is_exclusive():
    strict = Policy({**POLICY, "merchants": {"allow": ["bluebottlecoffee.com"], "deny": []}})
    assert strict.evaluate(make_request(), NO_SPEND).verdict is Verdict.ALLOWED
    other = make_request(merchant_url="https://amazon.com")
    assert strict.evaluate(other, NO_SPEND).rule_id == "merchant-allow"


def test_single_purchase_cap(policy):
    req = make_request(
        products=[Product(description="Espresso machine", unit_price=Decimal("30.00"))],
        total_amount=Decimal("30.00"),
    )
    assert policy.evaluate(req, NO_SPEND).rule_id == "max-single-purchase"


def test_daily_budget_counts_prior_spend(policy):
    nearly_spent = SpendContext(spent_today=Decimal("48.00"), purchases_in_window=0)
    assert policy.evaluate(make_request(), nearly_spent).rule_id == "daily-budget"


def test_velocity_limit(policy):
    busy = SpendContext(spent_today=Decimal("0"), purchases_in_window=3)
    assert policy.evaluate(make_request(), busy).rule_id == "velocity"


def test_large_amount_escalates_to_human():
    high_cap = Policy(
        {**POLICY, "agents": {"shopper": {"daily_budget": "500.00", "max_single_purchase": "500.00"}}}
    )
    req = make_request(
        products=[Product(description="Espresso machine", unit_price=Decimal("99.00"))],
        total_amount=Decimal("99.00"),
    )
    decision = high_cap.evaluate(req, NO_SPEND)
    assert decision.verdict is Verdict.NEEDS_APPROVAL
    assert decision.rule_id == "human-approval"
