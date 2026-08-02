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


def test_answered_at_records_when_a_human_answered(store):
    """`ts` is when the limit fired; `answered_at` is when somebody answered.

    The gap between them is the only measure of whether a threshold is
    protecting anyone or just taxing them, so releasing must stamp it and must
    leave `ts` alone.
    """
    attempt_id = store.record(make_request("150.00"), PENDING)
    parked = store.get(attempt_id)

    store.mark_released(attempt_id, "ses_9", "https://pay/9")

    released = store.get(attempt_id)
    assert released["ts"] == parked["ts"]
    assert datetime.fromisoformat(released["answered_at"]) >= datetime.fromisoformat(parked["ts"])


def test_answered_at_is_not_backfilled_from_ts(tmp_path):
    """The deliberate absence, and the mirror image of the base_amount backfill.

    `total_amount` was a correct substitute for a single-currency row. There is
    no correct substitute for a timestamp nobody recorded — reusing `ts` would
    claim every historical release was instant, which is a fabricated metric
    rather than a missing one.
    """
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
    # Already released under the old schema: allowed, but carrying the rule that
    # parked it — the only fingerprint of "escalated then released".
    conn.execute(
        "INSERT INTO attempts VALUES ('old1','2026-07-25T00:00:00+00:00','shopper',"
        "'Blue Bottle Coffee','https://bluebottlecoffee.com','150.00','USD',"
        "'allowed','human-approval','big','ses_old','https://pay/old')"
    )
    conn.commit()
    conn.close()

    store = AuditStore(db_path)

    assert store.get("old1")["answered_at"] == ""


def test_rows_are_marked_live_by_default(store):
    """Provenance rides on the row, not the file, because a demo database is
    realistically mixed: a real run and then a simulated sweep into one file."""
    attempt_id = store.record(make_request(), ALLOWED)

    assert store.get(attempt_id)["source"] == "live"


def test_the_engines_hot_path_is_indexed(store):
    """spent_today and purchases_in_window run on every purchase decision and
    were full scans. The reporting layer is the secondary beneficiary."""
    names = {row[1] for row in store._conn.execute("PRAGMA index_list(attempts)")}

    assert "idx_attempts_agent_verdict_ts" in names
    assert "idx_attempts_ts" in names


def test_a_refusal_is_terminal_and_mints_nothing(store):
    """The point of recording a no: no money moved and none can later, because
    the row is no longer pending and only a pending row can be released."""
    attempt_id = store.record(make_request("150.00"), PENDING)

    assert store.mark_rejected(attempt_id, "not this month") is True

    refused = store.get(attempt_id)
    assert refused["verdict"] == Verdict.REJECTED.value
    assert refused["session_id"] is None
    assert refused["answer_note"] == "not this month"
    assert store.spent_today("shopper") == Decimal("0")


def test_a_refusal_leaves_the_policys_own_wording_intact(store):
    """The rule that fired and the answer a person gave are different sentences
    and a spender is owed both. Neither may be paraphrased into the other."""
    attempt_id = store.record(make_request("150.00"), PENDING)

    store.mark_rejected(attempt_id, "we already have one")

    refused = store.get(attempt_id)
    assert refused["reason"] == "big"
    assert refused["answer_note"] == "we already have one"


def test_only_a_pending_attempt_can_be_refused(store):
    """Guarded in the WHERE clause rather than by a read-then-write, so a
    refusal racing an approval cannot both succeed."""
    attempt_id = store.record(make_request("150.00"), PENDING)
    store.mark_released(attempt_id, "ses_9", "https://pay/9")

    assert store.mark_rejected(attempt_id) is False
    assert store.get(attempt_id)["verdict"] == Verdict.ALLOWED.value


def test_a_refused_attempt_cannot_then_be_released(store):
    """The other direction of the same race."""
    attempt_id = store.record(make_request("150.00"), PENDING)
    store.mark_rejected(attempt_id)

    store.mark_released(attempt_id, "ses_9", "https://pay/9")

    assert store.get(attempt_id)["verdict"] == Verdict.REJECTED.value
    assert store.get(attempt_id)["session_id"] is None


def test_answered_at_is_carried_over_from_the_old_column_name(tmp_path):
    """`answered_at` was `released_at` while releasing was the only answer a
    person could give. Copying it across is legitimate — a release *is* an
    answer — which is the opposite judgement to refusing to backfill from `ts`."""
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE attempts (id TEXT PRIMARY KEY, ts TEXT NOT NULL, agent TEXT NOT NULL,"
        " merchant_name TEXT NOT NULL, merchant_url TEXT NOT NULL, total_amount TEXT NOT NULL,"
        " currency TEXT NOT NULL, verdict TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " reason TEXT NOT NULL, session_id TEXT, payment_url TEXT,"
        " released_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO attempts VALUES ('old1','2026-07-25T00:00:00+00:00','shopper','Blue Bottle',"
        "'https://bluebottlecoffee.com','150.00','USD','allowed','human-approval','big',"
        "'ses_old','https://pay/old','2026-07-25T00:12:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = AuditStore(db_path)

    assert store.get("old1")["answered_at"] == "2026-07-25T00:12:00+00:00"
