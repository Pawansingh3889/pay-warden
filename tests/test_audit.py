import json
import sqlite3
from datetime import UTC, datetime
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


def test_attempt_persists_country_and_products(store):
    attempt_id = store.record(make_request("150.00"), PENDING)
    attempt = store.get(attempt_id)
    assert attempt["merchant_country"] == "US"
    assert json.loads(attempt["products"]) == [
        {"description": "Latte", "unit_price": "150.00", "quantity": 1}
    ]


def test_migrates_db_created_before_country_and_products(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE attempts (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, agent TEXT NOT NULL,
            merchant_name TEXT NOT NULL, merchant_url TEXT NOT NULL,
            total_amount TEXT NOT NULL, currency TEXT NOT NULL,
            verdict TEXT NOT NULL, rule_id TEXT NOT NULL, reason TEXT NOT NULL,
            session_id TEXT, payment_url TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO attempts VALUES ('old1','2026-07-25T00:00:00+00:00','shopper',"
        "'Blue Bottle Coffee','https://bluebottlecoffee.com','5.00','USD',"
        "'needs_approval','human-approval','big',NULL,NULL)"
    )
    conn.commit()
    conn.close()

    store = AuditStore(db_path)
    legacy = store.get("old1")
    assert legacy["merchant_country"] == ""
    assert json.loads(legacy["products"]) == []

    # New rows land in the migrated table with both fields populated.
    attempt_id = store.record(make_request(), ALLOWED)
    assert store.get(attempt_id)["merchant_country"] == "US"


def test_spend_uses_base_amount_not_the_requested_currency(store):
    """Budgets are enforced in base currency, so that is what spend must sum."""
    store.record(make_request("20.00"), ALLOWED, "ses_1", "https://pay/1",
                 base_amount=Decimal("40.00"))
    assert store.spent_today("shopper") == Decimal("40.00")


def test_migration_backfills_base_amount_from_total(tmp_path):
    """Adding the column must not silently zero out existing spend history."""
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE attempts (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, agent TEXT NOT NULL,
            merchant_name TEXT NOT NULL, merchant_url TEXT NOT NULL,
            total_amount TEXT NOT NULL, currency TEXT NOT NULL,
            verdict TEXT NOT NULL, rule_id TEXT NOT NULL, reason TEXT NOT NULL,
            session_id TEXT, payment_url TEXT
        )
        """
    )
    conn.execute(
        f"INSERT INTO attempts VALUES ('old1','{datetime.now(UTC).isoformat()}','shopper',"
        "'Blue Bottle Coffee','https://bluebottlecoffee.com','9.00','USD',"
        "'allowed','pass','ok',NULL,NULL)"
    )
    conn.commit()
    conn.close()

    store = AuditStore(db_path)
    assert store.get("old1")["base_amount"] == "9.00"
    assert store.spent_today("shopper") == Decimal("9.00")


def test_recent_filters_by_agent(store):
    store.record(make_request(), ALLOWED)
    other = make_request()
    other = other.model_copy(update={"agent": "other-agent"})
    store.record(other, DENIED)

    assert len(store.recent()) == 2
    assert [r["agent"] for r in store.recent(agent="shopper")] == ["shopper"]
