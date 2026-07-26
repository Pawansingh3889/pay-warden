from decimal import Decimal

import pytest

from pay_warden.audit import AuditStore
from pay_warden.models import Decision, Product, PurchaseRequest, Verdict


@pytest.fixture
def store(tmp_path) -> AuditStore:
    return AuditStore(tmp_path / "audit.sqlite3")


def make_request(amount: str = "5.00") -> PurchaseRequest:
    return PurchaseRequest(
        agent="shopper",
        merchant_name="Blue Bottle Coffee",
        merchant_url="https://bluebottlecoffee.com",
        merchant_country="US",
        products=[Product(description="Latte", unit_price=Decimal(amount))],
        total_amount=Decimal(amount),
        currency="USD",
    )


ALLOWED = Decision(verdict=Verdict.ALLOWED, rule_id="pass", reason="ok")
DENIED = Decision(verdict=Verdict.DENIED, rule_id="daily-budget", reason="over budget")
PENDING = Decision(verdict=Verdict.NEEDS_APPROVAL, rule_id="human-approval", reason="big")


def test_only_allowed_attempts_count_as_spend(store):
    store.record(make_request("5.00"), ALLOWED, "ses_1", "https://pay/1")
    store.record(make_request("7.00"), DENIED)
    assert store.spent_today("shopper") == Decimal("5.00")
    assert store.purchases_in_window("shopper", 60) == 1


def test_release_pending_attempt(store):
    attempt_id = store.record(make_request("150.00"), PENDING)
    assert store.spent_today("shopper") == Decimal("0")

    store.mark_released(attempt_id, "ses_9", "https://pay/9")
    released = store.get(attempt_id)
    assert released["verdict"] == Verdict.ALLOWED.value
    assert released["session_id"] == "ses_9"
    assert store.spent_today("shopper") == Decimal("150.00")


def test_recent_filters_by_agent(store):
    store.record(make_request(), ALLOWED)
    other = make_request()
    other = other.model_copy(update={"agent": "other-agent"})
    store.record(other, DENIED)

    assert len(store.recent()) == 2
    assert [r["agent"] for r in store.recent(agent="shopper")] == ["shopper"]
