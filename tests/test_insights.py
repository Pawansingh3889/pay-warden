"""The metric contract.

These test definitions rather than plumbing. Each name states a property the
number on the page is claimed to have, so a failure here means the dashboard is
saying something untrue — which is worse than it being broken.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pay_warden import insights
from pay_warden.audit import AuditStore
from pay_warden.models import Decision, Product, PurchaseRequest, Verdict
from pay_warden.policy import Policy, SpendContext

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

POLICY = {
    "version": 1,
    "currencies": ["USD", "GBP"],
    "base_currency": "USD",
    "rates": {"USD": "1.00", "GBP": "1.25"},
    "agents": {
        "shopper": {"daily_budget": "500.00", "max_single_purchase": "250.00"},
    },
    "merchants": {"allow": [], "deny": ["*.casino.example"]},
    "velocity": {"max_purchases": 100, "window_minutes": 60},
    "human_approval_over": "100.00",
}


@pytest.fixture
def policy() -> Policy:
    return Policy(POLICY)


@pytest.fixture
def db(tmp_path):
    """A migrated database, and a raw connection for planting rows.

    Rows go in by direct INSERT because `record()` stamps `ts` from the clock,
    so history cannot be authored through it — the same reason the simulator
    writes directly.
    """
    path = tmp_path / "audit.sqlite3"
    AuditStore(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def plant(
    conn,
    *,
    ident="a1",
    ts=NOW,
    agent="shopper",
    url="https://shop.example/x",
    total="10.00",
    currency="USD",
    verdict="allowed",
    rule_id="pass",
    session="ses_1",
    base=None,
    released_at="",
    source="live",
):
    conn.execute(
        "INSERT INTO attempts (id, ts, agent, merchant_name, merchant_url, total_amount,"
        " currency, verdict, rule_id, reason, session_id, payment_url, merchant_country,"
        " products, base_amount, released_at, source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ident,
            ts.isoformat() if isinstance(ts, datetime) else ts,
            agent,
            "Shop",
            url,
            total,
            currency,
            verdict,
            rule_id,
            "because",
            session,
            "https://pay.example/1" if session else None,
            "US",
            "[]",
            base if base is not None else total,
            released_at,
            source,
        ),
    )
    conn.commit()


# --- the aggregates are real --------------------------------------------------


def test_aggregates_are_not_capped_by_the_feed_limit(db, policy):
    """The tiles used to be computed from the most recent 200 rows and rendered
    as totals — true only while the table is smaller than the page."""
    for i in range(250):
        plant(db, ident=f"r{i}", ts=NOW - timedelta(minutes=i))

    state = insights.build(db, policy, now=NOW, feed_limit=200)

    assert state["totals"]["attempts"] == 250
    assert len(state["feed"]["rows"]) == 200
    assert state["feed"]["total"] == 250


def test_base_amount_sums_are_exact(db, policy):
    """Three tenpences are thirty pence. SQLite's SUM() over a TEXT column
    coerces to float and yields 0.30000000000000004."""
    for i in range(3):
        plant(db, ident=f"r{i}", verdict="allowed", rule_id="human-approval", base="0.10")

    state = insights.build(db, policy, now=NOW)

    assert state["escalation"]["friction_cost"] == "0.30"


def test_money_aggregates_exclude_currencies_the_policy_cannot_price(db, policy):
    """`base_amount` holds a raw foreign amount when no rate was known, because
    server.py falls back to total_amount. Summing it would blend currencies."""
    plant(db, ident="ok", verdict="allowed", rule_id="human-approval", base="100.00")
    plant(
        db,
        ident="jpy",
        verdict="allowed",
        rule_id="human-approval",
        currency="JPY",
        total="5000",
        base="5000",
    )

    state = insights.build(db, policy, now=NOW)

    assert state["escalation"]["friction_cost"] == "100.00"
    assert state["escalation"]["unpriceable_rows"] == 1


# --- the escalation funnel ----------------------------------------------------


def test_release_rate_treats_pending_as_unresolved_not_rejected(db, policy):
    """There is no rejection state, so a human who said no and a human who has
    not looked leave identical rows. The rate is a lower bound."""
    plant(db, ident="held", verdict="needs_approval", rule_id="human-approval", session=None)
    plant(db, ident="out", verdict="allowed", rule_id="human-approval")

    escalation = insights.build(db, policy, now=NOW)["escalation"]

    assert escalation["raised"] == 2
    assert escalation["released"] == 1
    assert escalation["pending"] == 1
    assert escalation["release_rate"] == 0.5


def test_a_released_escalation_is_identified_by_its_rule_not_its_verdict(db, policy):
    """`mark_released` flips the verdict and leaves rule_id alone, so
    (allowed, human-approval) is the only fingerprint of "held, then let through".
    An ordinary allowed purchase must not be counted as one."""
    plant(db, ident="normal", verdict="allowed", rule_id="pass", base="900.00")
    plant(db, ident="released", verdict="allowed", rule_id="human-approval", base="150.00")

    escalation = insights.build(db, policy, now=NOW)["escalation"]

    assert escalation["raised"] == 1
    assert escalation["friction_cost"] == "150.00"


def test_friction_cost_excludes_what_is_still_held(db, policy):
    """Friction cost is money the limit *delayed*. Money still waiting has not
    been delayed-and-released yet, and belongs in its own figure."""
    plant(db, ident="out", verdict="allowed", rule_id="human-approval", base="40.00")
    plant(
        db,
        ident="held",
        verdict="needs_approval",
        rule_id="human-approval",
        base="60.00",
        session=None,
    )

    escalation = insights.build(db, policy, now=NOW)["escalation"]

    assert escalation["friction_cost"] == "40.00"
    assert escalation["value_held_now"] == "60.00"


def test_latency_reports_its_population_not_just_a_median(db, policy):
    """A median over an unstated population is a lie by omission: `released_at`
    cannot be backfilled, so some releases will never have a time."""
    plant(
        db,
        ident="timed",
        verdict="allowed",
        rule_id="human-approval",
        ts=NOW - timedelta(hours=2),
        released_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    plant(db, ident="untimed", verdict="allowed", rule_id="human-approval", released_at="")

    latency = insights.build(db, policy, now=NOW)["escalation"]["latency"]

    assert latency["available"] is True
    assert latency["n"] == 1
    assert latency["untimed"] == 1
    assert latency["median_s"] == 3600


def test_a_negative_wait_is_discarded_and_counted(db, policy):
    """A release before its request is a clock or generator bug, never a fact."""
    plant(
        db,
        ident="backwards",
        verdict="allowed",
        rule_id="human-approval",
        ts=NOW,
        released_at=(NOW - timedelta(hours=1)).isoformat(),
    )

    latency = insights.build(db, policy, now=NOW)["escalation"]["latency"]

    assert latency["n"] == 0
    assert latency["invalid"] == 1
    assert latency["median_s"] is None


def test_latency_says_so_on_a_database_that_predates_the_column(tmp_path, policy):
    """The dashboard opens the audit DB read-only and cannot migrate it, so
    pointing it at an older file must report the gap, not crash."""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE attempts (id TEXT PRIMARY KEY, ts TEXT NOT NULL, agent TEXT NOT NULL,"
        " merchant_name TEXT NOT NULL, merchant_url TEXT NOT NULL, total_amount TEXT NOT NULL,"
        " currency TEXT NOT NULL, verdict TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " reason TEXT NOT NULL, session_id TEXT, payment_url TEXT, base_amount TEXT"
        " NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row

    state = insights.build(conn, policy, now=NOW)

    assert state["meta"]["capabilities"]["released_at"] is False
    assert state["escalation"]["latency"]["available"] is False


# --- the counterfactual -------------------------------------------------------


def test_the_threshold_curve_matches_what_the_engine_would_have_done(db, policy):
    """The strong one: the what-if must be a replay, not a model.

    Generate rows through the real engine at one threshold, then re-evaluate the
    same requests under a policy with a different threshold, and assert the
    curve's prediction equals what the engine actually did.
    """
    amounts = ["50.00", "120.00", "180.00", "260.00", "90.00"]
    for i, amount in enumerate(amounts):
        request = PurchaseRequest(
            agent="shopper",
            merchant_name="Shop",
            merchant_url="https://shop.example",
            merchant_country="US",
            products=[Product(description="thing", unit_price=Decimal(amount))],
            total_amount=Decimal(amount),
            currency="USD",
        )
        decision = policy.evaluate(request, SpendContext(Decimal("0"), 0))
        plant(
            db,
            ident=f"r{i}",
            ts=NOW - timedelta(minutes=i),
            total=amount,
            base=amount,
            verdict=decision.verdict.value,
            rule_id=decision.rule_id,
            session="ses" if decision.verdict is Verdict.ALLOWED else None,
        )

    curve = insights.build(db, policy, now=NOW)["threshold"]["curve"]
    assert curve, "a populated window must produce candidate thresholds"

    for point in curve:
        moved = Policy({**POLICY, "human_approval_over": point["threshold"]})
        would_escalate = sum(
            1
            for amount in amounts
            if moved.evaluate(
                PurchaseRequest(
                    agent="shopper",
                    merchant_name="Shop",
                    merchant_url="https://shop.example",
                    merchant_country="US",
                    products=[Product(description="thing", unit_price=Decimal(amount))],
                    total_amount=Decimal(amount),
                    currency="USD",
                ),
                SpendContext(Decimal("0"), 0),
            ).verdict
            is Verdict.NEEDS_APPROVAL
        )

        assert point["escalations"] == would_escalate, point["threshold"]


def test_the_threshold_population_excludes_rows_stopped_earlier(db, policy):
    """A request denied by the merchant rule never reached the threshold, so
    moving the threshold could not have changed it."""
    plant(db, ident="blocked", verdict="denied", rule_id="merchant-deny", base="900.00")
    plant(db, ident="reached", verdict="allowed", rule_id="pass", base="10.00")

    assert insights.build(db, policy, now=NOW)["threshold"]["population"] == 1


# --- denial pressure ----------------------------------------------------------


def test_denials_are_reported_as_stopped_by_not_violated(db, policy):
    """`Policy.evaluate` is first-match-wins, so a request that broke two rules
    is attributed only to the first. The payload key has to say so."""
    plant(db, ident="d1", verdict="denied", rule_id="merchant-deny", base="900.00")

    rules = insights.build(db, policy, now=NOW)["rules"]

    assert "stopped_by" in rules
    assert rules["stopped_by"][0]["rule_id"] == "merchant-deny"


def test_a_rule_outside_the_vocabulary_is_bucketed_rather_than_dropped(db, policy):
    """The second-opinion hook can return any rule_id and the column has no
    constraint, so an unrecognised value must survive to the page."""
    plant(db, ident="odd", verdict="denied", rule_id="second-opinion:prompt-injection")

    rules = insights.build(db, policy, now=NOW)["rules"]

    assert [r["rule_id"] for r in rules["outside_vocabulary"]] == [
        "second-opinion:prompt-injection"
    ]


def test_adaptation_counts_a_cohort_and_never_claims_a_rate(db, policy):
    """Blocked, then a smaller purchase at the same merchant soon after. Real,
    descriptive, and deliberately not phrased as causation."""
    plant(
        db,
        ident="blocked",
        ts=NOW - timedelta(minutes=30),
        verdict="denied",
        rule_id="max-single-purchase",
        base="300.00",
        session=None,
    )
    plant(db, ident="smaller", ts=NOW - timedelta(minutes=10), verdict="allowed", base="80.00")

    adaptation = insights.build(db, policy, now=NOW)["rules"]["adaptation"]

    assert adaptation["denied"] == 1
    assert adaptation["followed_by_smaller_allowed"] == 1
    assert "rate" not in adaptation


def test_a_larger_later_purchase_is_not_adaptation(db, policy):
    plant(
        db,
        ident="blocked",
        ts=NOW - timedelta(minutes=30),
        verdict="denied",
        rule_id="max-single-purchase",
        base="300.00",
        session=None,
    )
    plant(db, ident="bigger", ts=NOW - timedelta(minutes=10), verdict="allowed", base="400.00")

    assert (
        insights.build(db, policy, now=NOW)["rules"]["adaptation"][
            "followed_by_smaller_allowed"
        ]
        == 0
    )


# --- activation ---------------------------------------------------------------


def test_unregistered_agents_appear_although_the_policy_never_heard_of_them(db, policy):
    """The previous page iterated the policy file for agents, so the population
    the activation panel is about was invisible on it."""
    plant(db, ident="u1", agent="skunkworks:proto", verdict="denied", rule_id="unknown-agent")

    state = insights.build(db, policy, now=NOW)

    assert state["activation"]["identities"] == 1
    assert state["activation"]["stuck"] == 1
    assert any(a["agent"] == "skunkworks:proto" and not a["registered"] for a in state["agents"])


def test_an_identity_that_later_registers_is_counted_as_recovered(db, policy):
    plant(
        db,
        ident="u1",
        ts=NOW - timedelta(hours=2),
        agent="acme:bot",
        verdict="denied",
        rule_id="unknown-agent",
    )
    plant(db, ident="ok", ts=NOW - timedelta(hours=1), agent="acme:bot", rule_id="pass")

    activation = insights.build(db, policy, now=NOW)["activation"]

    assert activation["recovered"] == 1
    assert activation["stuck"] == 0
    assert activation["median_seconds_to_registration"] == 3600


# --- the engine and the page must agree ---------------------------------------


def test_reported_spend_matches_what_the_engine_enforces(db, policy, tmp_path):
    """The metric layer computes spend itself, with an injected clock, while the
    engine uses its own. That duplication is deliberate — but the two disagreeing
    about what an agent spent would be the worst possible bug on this page."""
    plant(db, ident="a", ts=NOW, verdict="allowed", base="120.00")
    plant(db, ident="b", ts=NOW, verdict="allowed", base="30.00")
    plant(db, ident="denied", ts=NOW, verdict="denied", rule_id="daily-budget", base="900.00")

    reported = next(
        a for a in insights.build(db, policy, now=NOW)["agents"] if a["agent"] == "shopper"
    )

    # The engine's own reader, over the same rows, at the same UTC day.
    engine_total = sum(
        (
            Decimal(row["base_amount"])
            for row in db.execute(
                "SELECT base_amount FROM attempts WHERE agent = ? AND verdict = 'allowed'"
                " AND ts >= ?",
                ("shopper", NOW.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()),
            )
        ),
        Decimal("0"),
    )
    assert reported["spent"] == str(engine_total.quantize(Decimal("0.01")))


def test_now_is_injected_so_a_window_is_deterministic(db, policy):
    """Anything older than the window is out of the totals, whatever the clock
    on the machine says."""
    plant(db, ident="recent", ts=NOW - timedelta(days=2))
    plant(db, ident="ancient", ts=NOW - timedelta(days=90))

    assert insights.build(db, policy, now=NOW, window_days=30)["totals"]["attempts"] == 1


# --- provenance ---------------------------------------------------------------


def test_every_panel_carries_its_own_provenance(db, policy):
    """The structural guarantee behind the SIMULATED label: the renderer builds
    each heading from this, so a panel cannot forget to declare itself, and a
    crop that removes the label removes the number it qualifies."""
    plant(db, ident="sim", source="simulated")

    state = insights.build(db, policy, now=NOW)

    for name in ("escalation", "threshold", "rules", "activation"):
        assert "provenance" in state[name], name
        assert set(state[name]["provenance"]) >= {"simulated", "total"}
    assert state["meta"]["provenance"]["label"] == "SIMULATED"


def test_a_mixed_database_is_labelled_mixed_not_simulated(db, policy):
    """The realistic state once a real run and a simulated sweep share a file."""
    plant(db, ident="real", source="live")
    plant(db, ident="fake", source="simulated")

    assert insights.build(db, policy, now=NOW)["meta"]["provenance"]["label"] == "MIXED"


def test_value_is_never_blended_across_currencies(db, policy):
    """No FX rate is stored, so a single cross-currency total would be invented."""
    plant(db, ident="usd", total="10.00", currency="USD")
    plant(db, ident="gbp", total="20.00", currency="GBP")

    assert insights.build(db, policy, now=NOW)["totals"]["authorised"] == {
        "GBP": "20.00",
        "USD": "10.00",
    }
