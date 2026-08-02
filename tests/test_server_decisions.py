"""The two things a person can do to a parked request, through the MCP tools.

`reject_purchase` exists because silence and a decision look identical in an
audit trail. A spender waiting on an answer nobody recorded is the worst
outcome this flow can produce, and until there was a rejected verdict it was
also the *only* way to say no.
"""

import importlib
from decimal import Decimal

import pytest

from pay_warden.models import Product, PurchaseRequest, Verdict


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A server bound to a throwaway store.

    `_policy` and `_audit` are module-level, so the module must be reloaded
    after the environment is set — unlike the dashboard, which reads its config
    per request and needs no reload.
    """
    monkeypatch.setenv("PAY_WARDEN_DB", str(tmp_path / "audit.sqlite3"))
    monkeypatch.setenv("PAY_WARDEN_POLICY", "policies/default.yaml")
    module = importlib.reload(importlib.import_module("pay_warden.server"))
    yield module
    # Leave the module bound to the real environment for anything after us.
    importlib.reload(module)


def park(server, amount: str = "150.00") -> str:
    """Record an attempt that the policy parks for a human."""
    request = PurchaseRequest(
        agent="ops-agent",
        merchant_name="Example Coffee Roasters",
        merchant_url="https://example.com",
        merchant_country="US",
        products=[Product(description="Beans", unit_price=Decimal(amount))],
        total_amount=Decimal(amount),
        currency="USD",
    )
    decision = server._evaluate(request)
    assert decision.verdict is Verdict.NEEDS_APPROVAL, decision.reason
    return server._audit.record(request, decision)


def test_refusing_records_a_decision_and_mints_nothing(server):
    attempt_id = park(server)

    result = server.reject_purchase(attempt_id, note="we already have one")

    assert result["rejected"] is True
    row = server._audit.get(attempt_id)
    assert row["verdict"] == Verdict.REJECTED.value
    assert row["session_id"] is None
    assert row["answered_at"]


def test_the_refusal_carries_back_the_rule_that_parked_it(server):
    """A spender is owed both sentences: why it was held, and what was decided.
    Neither is a paraphrase of the other."""
    attempt_id = park(server)

    result = server.reject_purchase(attempt_id, note="not this month")

    assert "auto-approval threshold" in result["reason"]
    assert result["note"] == "not this month"


def test_a_refused_attempt_can_no_longer_be_approved(server):
    """Terminal, and the guard is the same one approve_purchase already used."""
    attempt_id = park(server)
    server.reject_purchase(attempt_id)

    result = server.approve_purchase(attempt_id)

    assert "not pending approval" in result["error"]
    assert server._audit.get(attempt_id)["verdict"] == Verdict.REJECTED.value


def test_refusing_something_already_decided_is_an_error_not_a_second_decision(server):
    attempt_id = park(server)
    server.reject_purchase(attempt_id)

    result = server.reject_purchase(attempt_id, note="again")

    assert "not pending approval" in result["error"]
    assert server._audit.get(attempt_id)["answer_note"] == ""


def test_refusing_an_unknown_attempt_says_so(server):
    assert "No attempt" in server.reject_purchase("nope")["error"]


def test_a_refusal_never_counts_toward_spend(server):
    """It is not a purchase. The budget must be untouched by one."""
    attempt_id = park(server)

    server.reject_purchase(attempt_id)

    assert server._audit.spent_today("ops-agent") == Decimal("0")


def test_the_policy_engine_never_produces_a_rejection(server):
    """Only a person does. A denial is the engine refusing under a rule and
    nobody chose it; conflating them would tell a spender their sponsor said no
    when in fact a limit did."""
    request = PurchaseRequest(
        agent="rogue-agent",
        merchant_name="Sketchy",
        merchant_url="https://sketchy-deals.example",
        merchant_country="US",
        products=[Product(description="thing", unit_price=Decimal("4.00"))],
        total_amount=Decimal("4.00"),
        currency="USD",
    )

    assert server._evaluate(request).verdict is not Verdict.REJECTED
