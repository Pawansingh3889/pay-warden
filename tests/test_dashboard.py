import importlib
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from pay_warden.audit import AuditStore
from pay_warden.models import Decision, Product, PurchaseRequest, Verdict


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Point the dashboard at a throwaway store seeded with one of each verdict."""
    db = tmp_path / "audit.sqlite3"
    store = AuditStore(db)

    def request(agent: str, amount: str, url: str = "https://example.com") -> PurchaseRequest:
        return PurchaseRequest(
            agent=agent,
            merchant_name="Example Coffee Roasters",
            merchant_url=url,
            merchant_country="US",
            products=[Product(description="Beans", unit_price=Decimal(amount))],
            total_amount=Decimal(amount),
            currency="USD",
        )

    store.record(
        request("demo-shopper", "18.50"),
        Decision(verdict=Verdict.ALLOWED, rule_id="pass", reason="ok"),
        "ses_1",
        "https://pay.example/1",
    )
    store.record(
        request("rogue-agent", "4.00", "https://sketchy-deals.example"),
        Decision(verdict=Verdict.DENIED, rule_id="merchant-deny", reason="denied merchant"),
    )
    store.record(
        request("ops-agent", "150.00"),
        Decision(verdict=Verdict.NEEDS_APPROVAL, rule_id="human-approval", reason="big"),
    )

    monkeypatch.setenv("PAY_WARDEN_DB", str(db))
    monkeypatch.setenv("PAY_WARDEN_POLICY", "policies/default.yaml")
    dashboard = importlib.import_module("pay_warden.dashboard")
    return TestClient(dashboard.app)


def test_index_serves_the_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "pay-warden" in response.text


def test_state_counts_each_verdict(client):
    summary = client.get("/api/state").json()["summary"]
    assert summary["attempts"] == 3
    assert summary["allowed"] == 1
    assert summary["denied"] == 1
    assert summary["needs_approval"] == 1
    assert summary["sessions"] == 1


def test_minted_volume_is_reported_per_currency(client):
    """Blending currencies into one total would be a fiction, so the payload
    keeps them separate even though the policy engine sums them."""
    minted = client.get("/api/state").json()["summary"]["minted"]
    assert minted == {"USD": "18.50"}


def test_only_allowed_attempts_count_toward_spend(client):
    agents = {a["agent"]: a for a in client.get("/api/state").json()["agents"]}
    assert agents["demo-shopper"]["spent"] == "18.50"
    # Denied and pending attempts must not consume budget.
    assert agents["rogue-agent"]["spent"] == "0"
    assert agents["ops-agent"]["spent"] == "0"


def test_denial_rules_are_summarised(client):
    rules = dict(tuple(r) for r in client.get("/api/state").json()["rules"])
    assert rules == {"merchant-deny": 1}


def test_attempts_carry_what_the_table_renders(client):
    attempt = client.get("/api/state").json()["attempts"][0]
    for field in ("ts", "agent", "merchant_name", "total_amount", "currency",
                  "verdict", "rule_id", "reason", "session_id", "payment_url"):
        assert field in attempt
