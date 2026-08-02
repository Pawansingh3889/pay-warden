import importlib
import re
from decimal import Decimal
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pay_warden.audit import AuditStore
from pay_warden.models import Decision, Product, PurchaseRequest, Verdict

PAGE = Path(__file__).resolve().parents[1] / "src" / "pay_warden" / "dashboard.html"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Point the dashboard at a throwaway store seeded with one of each verdict.

    `importlib.import_module` returns the cached module, which is harmless only
    because the connection and policy are opened per request. If either is ever
    cached at module level this must become `importlib.reload`, or every test
    silently binds to the first one's tmp_path.
    """
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
    for name in ("PAY_WARDEN_CANIBUY_DB", "CANIBUY_DB"):
        monkeypatch.delenv(name, raising=False)
    dashboard = importlib.import_module("pay_warden.dashboard")
    return TestClient(dashboard.app)


def test_index_serves_the_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "pay-warden" in response.text


def test_totals_count_each_verdict(client):
    totals = client.get("/api/state").json()["totals"]
    assert totals["attempts"] == 3
    assert totals["allowed"] == 1
    assert totals["denied"] == 1
    assert totals["needs_approval"] == 1
    assert totals["sessions"] == 1


def test_value_authorised_is_reported_per_currency(client):
    """Blending currencies into one total would be a fiction — no FX rate is
    stored — so the payload keeps them separate even though the engine sums
    them against a budget."""
    authorised = client.get("/api/state").json()["totals"]["authorised"]
    assert authorised == {"USD": "18.50"}


def test_only_allowed_attempts_count_toward_spend(client):
    agents = {a["agent"]: a for a in client.get("/api/state").json()["agents"]}
    assert agents["demo-shopper"]["spent"] == "18.50"
    # Denied and pending attempts must not consume budget.
    assert agents["rogue-agent"]["spent"] == "0.00"
    assert agents["ops-agent"]["spent"] == "0.00"


def test_denials_are_reported_as_what_stopped_them(client):
    """First-match-wins evaluation means these are requests each rule *stopped*,
    not requests that violated it."""
    stopped = client.get("/api/state").json()["rules"]["stopped_by"]
    assert [(r["rule_id"], r["n"]) for r in stopped] == [("merchant-deny", 1)]


def test_the_feed_carries_what_the_table_renders(client):
    """The contract binding this payload to the untested renderer."""
    row = client.get("/api/state").json()["feed"]["rows"][0]
    for field in ("ts", "agent", "merchant_name", "total_amount", "currency",
                  "verdict", "rule_id", "reason", "session_id", "payment_url",
                  "answered_at", "answer_note", "source"):
        assert field in row


def test_totals_are_not_the_feed(client):
    """The feed is capped; the totals are not. Rendering the cap as a total was
    true only while the table was smaller than the page."""
    payload = client.get("/api/state").json()
    assert payload["feed"]["limit"] == 200
    assert payload["feed"]["total"] == payload["totals"]["attempts"]
    assert "attempts" not in payload["feed"]


def test_every_panel_declares_its_own_provenance(client):
    """The structural guarantee behind the SIMULATED label: the renderer builds
    each heading from this, so a panel cannot forget to say where its numbers
    came from — and a crop that loses the label loses the number too."""
    payload = client.get("/api/state").json()
    for panel in ("escalation", "threshold", "rules", "activation"):
        assert set(payload[panel]["provenance"]) >= {"simulated", "total"}, panel
    assert payload["meta"]["provenance"]["label"] == ""


def test_the_merchant_panel_is_absent_until_configured(client):
    """canibuy is a sibling product, not a dependency. Not having it is normal."""
    merchants = client.get("/api/state").json()["merchants"]
    assert merchants["available"] is False
    assert merchants["reason"] == "not configured"


def test_the_page_renders_every_panel_the_payload_supplies(client):
    """Catches the file pair's biggest failure mode in both directions: a panel
    added to the payload and never rendered, or rendered and never sent."""
    payload = client.get("/api/state").json()
    rendered = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', PAGE.read_text(encoding="utf-8")))

    for panel in ("escalation", "threshold", "rules", "activation", "merchants", "feed"):
        assert panel in payload
        assert panel in rendered, f"payload has {panel} but the page never renders it"


def test_a_missing_database_is_a_message_not_a_stack_trace(tmp_path, monkeypatch):
    """No audit database yet is the normal state before the first purchase."""
    monkeypatch.setenv("PAY_WARDEN_DB", str(tmp_path / "never-created.sqlite3"))
    monkeypatch.setenv("PAY_WARDEN_POLICY", "policies/default.yaml")
    dashboard = importlib.import_module("pay_warden.dashboard")

    response = TestClient(dashboard.app, raise_server_exceptions=False).get("/api/state")

    assert response.status_code == 503
    assert "never-created.sqlite3" in response.json()["error"]


def test_the_dashboard_cannot_write_to_the_audit_database(client, tmp_path):
    """It shares a file with the policy engine. Opening it read-only is what
    makes the module docstring true, and stops a five-second poll from taking a
    write lock on the engine's own store."""
    import sqlite3

    from pay_warden import dashboard

    conn = dashboard._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM attempts")
    finally:
        conn.close()
