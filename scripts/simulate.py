#!/usr/bin/env python3
"""Generate an audit trail large enough to see the operator dashboard working.

    python scripts/simulate.py --days 30 --per-day 160

**It drives the real policy engine.** `scripts/demo.py` states the principle
this borrows: nothing here reimplements the rules. Every row below is a
`PurchaseRequest` evaluated by `Policy.evaluate` against a `SpendContext` built
at the simulated moment, so verdict, rule_id and base_amount are correct by
construction. Hand-authored rows would eventually produce a combination the
engine cannot reach — `allowed` with `velocity`, say — and one impossible row on
screen discredits every real one beside it.

Only two things are synthetic: the clock, and the write. `AuditStore.record`
stamps `ts` from `datetime.now`, so history cannot be authored through it and
rows are INSERTed directly — the same thing the audit tests already do.

**Every row is stamped `source='simulated'`.** Provenance rides on the row
rather than the file because the realistic state of a demo database is mixed: a
real run, then a sweep like this, into one file. The dashboard reads that column
per panel and puts a SIMULATED chip on the heading of anything it touched.

Nothing here calls Prava. Session ids are obviously fake and no payment URL is
invented — there is no such thing as a payment session that was never minted,
and a plausible-looking one on a dashboard screenshot would be the single most
misleading pixel in this project.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pay_warden.audit import AuditStore  # noqa: E402
from pay_warden.models import Decision, Product, PurchaseRequest, Verdict  # noqa: E402
from pay_warden.policy import Policy, SpendContext  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

# Registered spenders, and the tenant prefix convention the live database
# already uses. pay-warden treats `agent` as opaque; the colon is a caller's
# habit, which is exactly what the dashboard says about it.
REGISTERED = (
    "steward:person_2",
    "steward:person_3",
    "steward:person_7",
    "acme:concierge-1",
    "acme:concierge-2",
    "northwind:ops",
)
# Never added to the policy: these are the unregistered-identity panel.
NEVER_REGISTERED = ("skunkworks:proto-1", "skunkworks:proto-2", "hobby:weekend-bot")
# Denied at first, then registered part-way through the window, so
# time-to-registration has something to measure.
REGISTERS_LATE = ("bluesky:agent-1", "bluesky:agent-2")

# Hosts canibuy has actually graded, so the readiness join matches, plus a few
# it has never seen so coverage lands honestly below 100%.
GRADED_MERCHANTS = (
    ("Adafruit", "https://adafruit.com/product/1"),
    ("SparkFun", "https://sparkfun.com/products/2"),
    ("Blue Bottle Coffee", "https://bluebottlecoffee.com/store/x"),
    ("Allbirds", "https://allbirds.com/products/runner"),
    ("Target", "https://target.com/p/thing"),
    ("Etsy", "https://etsy.com/listing/9"),
)
UNGRADED_MERCHANTS = (
    ("Everyday Goods", "https://everyday.fixture.example/soap"),
    ("Corner Shop Express", "https://cornershop.fixture.example/paper"),
)
BLOCKED_MERCHANT = ("Lucky Spin", "https://parlour.casino.example/chips")

# What a person types when they refuse. Stored apart from the policy's reason,
# which stays exactly as the engine worded it.
NOTES = (
    "we already have one",
    "not this month",
    "cheaper option please",
    "ask me before anything this size",
    "wrong merchant",
)

ITEMS = ("coffee beans", "printer paper", "hand soap", "usb cable", "notebook",
         "desk lamp", "keyboard", "winter coat", "running shoes", "headphones")


def _amount(rng: random.Random) -> Decimal:
    """Log-normal, clustered below a typical threshold with a fat tail above it.

    A uniform spread would make the threshold what-if a straight line, which
    would look like a working chart and teach the reader nothing.
    """
    value = Decimal(str(round(rng.lognormvariate(3.6, 1.0), 2)))
    return max(Decimal("1.00"), min(value, Decimal("900.00")))


def _request(rng: random.Random, agent: str) -> PurchaseRequest:
    if rng.random() < 0.02:
        name, url = BLOCKED_MERCHANT
    elif rng.random() < 0.75:
        name, url = rng.choice(GRADED_MERCHANTS)
    else:
        name, url = rng.choice(UNGRADED_MERCHANTS)
    amount = _amount(rng)
    # A currency the policy cannot price, occasionally, so the unknown-rate path
    # and the dashboard's exclusion of unpriceable rows are both exercised.
    currency = "USD"
    roll = rng.random()
    if roll < 0.04:
        currency = "JPY"
    elif roll < 0.16:
        currency = rng.choice(("GBP", "EUR"))
    return PurchaseRequest(
        agent=agent,
        merchant_name=name,
        merchant_url=url,
        merchant_country="US",
        products=[Product(description=rng.choice(ITEMS), unit_price=amount)],
        total_amount=amount,
        currency=currency,
    )


class Ledger:
    """The spend history the engine would have seen at each simulated moment.

    Keyed by UTC day to match `spent_today`, and by a sliding deque to match
    `purchases_in_window`. A released escalation joins the ledger at its
    *release* time, not its request time, because that is what the real store
    does: `spent_today` counts rows that are allowed when it is asked.
    """

    def __init__(self) -> None:
        self.spent: dict[tuple[str, str], Decimal] = {}
        self.recent: dict[str, deque[datetime]] = {}

    def context(self, agent: str, when: datetime, window_minutes: int) -> SpendContext:
        day = when.date().isoformat()
        seen = self.recent.setdefault(agent, deque())
        cutoff = when - timedelta(minutes=window_minutes)
        while seen and seen[0] < cutoff:
            seen.popleft()
        return SpendContext(self.spent.get((agent, day), Decimal("0")), len(seen))

    def add(self, agent: str, when: datetime, base: Decimal) -> None:
        day = when.date().isoformat()
        self.spent[(agent, day)] = self.spent.get((agent, day), Decimal("0")) + base
        self.recent.setdefault(agent, deque()).append(when)


def _release_delay(rng: random.Random) -> timedelta:
    """How long a person took. Mostly minutes, a long tail into the next day."""
    roll = rng.random()
    if roll < 0.55:
        return timedelta(minutes=rng.uniform(1, 25))
    if roll < 0.85:
        return timedelta(hours=rng.uniform(0.5, 6))
    return timedelta(hours=rng.uniform(12, 40))


# Varied on purpose: identical limits make max-single-purchase and daily-budget
# fire at the same amounts for everybody, and the denial table goes flat.
LIMITS = {
    "steward:person_2": ("1200.00", "250.00"),
    "steward:person_3": ("600.00", "120.00"),
    "steward:person_7": ("2400.00", "400.00"),
    "acme:concierge-1": ("1500.00", "300.00"),
    "acme:concierge-2": ("400.00", "90.00"),
    "northwind:ops": ("3000.00", "500.00"),
    "bluesky:agent-1": ("1000.00", "200.00"),
    "bluesky:agent-2": ("1000.00", "200.00"),
}


def platform_policy(raw: dict) -> dict:
    """The seed policy, reshaped for a platform rather than a three-agent demo.

    Velocity is the one setting that has to move. `policies/default.yaml` allows
    three purchases an hour, which is a sensible demo limit and a nonsensical
    platform one: at any realistic traffic rate it fires before every other rule
    and the denial table becomes a single bar. Raising it is not tuning the
    output — the engine still decides — it is picking an input a real deployment
    would have picked.
    """
    agents = dict(raw.get("agents") or {})
    for name in (*REGISTERED, *REGISTERS_LATE):
        daily, single = LIMITS[name]
        agents[name] = {"daily_budget": daily, "max_single_purchase": single}
    return {
        **raw,
        "agents": agents,
        "velocity": {"max_purchases": 14, "window_minutes": 60},
    }


def policies(full: dict) -> tuple[Policy, Policy]:
    """The policy before and after the late registrants were added to it.

    An agent "registering" is a policy edit, not a property of the request, so
    the only faithful way to simulate one is to evaluate against the policy that
    was in force at that moment. Two Policy objects, chosen by timestamp.
    """
    before_agents = {
        name: limits
        for name, limits in full["agents"].items()
        if name not in REGISTERS_LATE
    }
    return Policy({**full, "agents": before_agents}), Policy(full)


def simulate(
    before: Policy, after: Policy, *, days: int, per_day: int, seed: int, end: datetime
) -> list[tuple]:
    rng = random.Random(seed)
    ledger = Ledger()
    rows: list[tuple] = []
    start = end - timedelta(days=days)
    # Half the window in, the late registrants start being recognised.
    registration_at = start + timedelta(days=days // 2)

    for day in range(days):
        midnight = (start + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Weekends are quieter; a flat rate makes the per-day chart noise.
        weekday_scale = 0.45 if midnight.weekday() >= 5 else 1.0
        for _ in range(int(per_day * weekday_scale)):
            # Business hours, roughly, so budget-exhaustion times mean something.
            when = midnight + timedelta(hours=min(23.99, abs(rng.gauss(13, 3.4))))
            if when > end:
                continue
            agent = _pick_agent(rng)
            policy = after if when >= registration_at else before
            request = _request(rng, agent)
            ctx = ledger.context(agent, when, policy.velocity_window_minutes)
            decision = policy.evaluate(request, ctx)
            base = policy.to_base(request.total_amount, request.currency)
            rows.append(_row(rng, request, decision, base, when, ledger))
    rows.sort(key=lambda r: r[1])
    return rows


def _pick_agent(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.08:
        return rng.choice(NEVER_REGISTERED)
    if roll < 0.14:
        return rng.choice(REGISTERS_LATE)
    return rng.choice(REGISTERED)


def _row(
    rng: random.Random,
    request: PurchaseRequest,
    decision: Decision,
    base: Decimal | None,
    when: datetime,
    ledger: Ledger,
) -> tuple:
    """One INSERT tuple, with the human's answer already decided.

    An escalation ends one of three ways, and each is written exactly as the
    store would leave it. `rule_id` stays `human-approval` throughout, because
    neither `mark_released` nor `mark_rejected` touches it — so within the
    escalation population the verdict alone says which way it went.
    """
    verdict = decision.verdict.value
    rule_id = decision.rule_id
    answered_at = ""
    answer_note = ""
    session_id = None

    if decision.verdict is Verdict.ALLOWED:
        session_id = f"ses_sim_{uuid.uuid4().hex[:10]}"
        if base is not None:
            ledger.add(request.agent, when, base)
    elif decision.verdict is Verdict.NEEDS_APPROVAL:
        answer = rng.random()
        if answer < 0.62:
            answered = when + _release_delay(rng)
            verdict = Verdict.ALLOWED.value
            answered_at = answered.isoformat()
            session_id = f"ses_sim_{uuid.uuid4().hex[:10]}"
            # At the release moment, not the request moment. Getting this wrong
            # would hide the late-release budget behaviour rather than
            # reproduce it.
            if base is not None:
                ledger.add(request.agent, answered, base)
        elif answer < 0.84:
            # Refused. No session, and nothing joins the ledger — a rejection is
            # not a purchase, and the budget must be untouched by one. People
            # also take longer to say no than to say yes, so the delay is drawn
            # twice and the longer one kept.
            verdict = Verdict.REJECTED.value
            answered_at = (
                when + max(_release_delay(rng), _release_delay(rng))
            ).isoformat()
            answer_note = rng.choice(NOTES)
        # The remainder stay `needs_approval`: a queue nobody has answered yet,
        # which is a real state and the reason release rate is reported over
        # what was answered rather than over what was raised.

    return (
        uuid.uuid4().hex[:12],
        when.isoformat(),
        request.agent,
        request.merchant_name,
        request.merchant_url,
        str(request.total_amount),
        request.currency,
        verdict,
        rule_id,
        decision.reason,
        session_id,
        # No payment_url, ever. Nothing here minted a session.
        None,
        request.merchant_country,
        "[]",
        str(base if base is not None else request.total_amount),
        answered_at,
        answer_note,
        "simulated",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="pay_warden_simulated.sqlite3")
    parser.add_argument("--policy", default="policies/default.yaml",
                        help="seed policy for currencies, rates and merchant rules")
    parser.add_argument("--write-policy", default="policies/simulated.yaml",
                        help="where to write the policy the traffic was judged against")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--per-day", type=int, default=160)
    # Fixed by default: a screenshot should be reproducible.
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    parser.add_argument(
        "--allow-mixed",
        action="store_true",
        help="write alongside rows that were not simulated",
    )
    args = parser.parse_args(argv)

    path = Path(args.db)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

    with open(args.policy, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    full = platform_policy(raw)
    before, after = policies(full)
    # Written out, because the dashboard reads a policy file for budgets and
    # caps. Without this every simulated agent would render as "not in policy",
    # which is exactly the state the activation panel is meant to make rare.
    written = Path(args.write_policy)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(
        "# Generated by scripts/simulate.py. The agents and velocity here are\n"
        "# what the simulated traffic was judged against; point PAY_WARDEN_POLICY\n"
        "# at this file so the dashboard reads the same limits the engine used.\n"
        + yaml.safe_dump(full, sort_keys=False),
        encoding="utf-8",
    )
    # Construct the store first so the schema and migrations run, then write
    # through a separate connection: `record` stamps its own `ts`.
    AuditStore(path)
    conn = sqlite3.connect(path)
    (live,) = conn.execute("SELECT COUNT(*) FROM attempts WHERE source <> 'simulated'").fetchone()
    if live and not args.allow_mixed:
        print(
            f"{path} already holds {live} row(s) that were not simulated."
            " Pass --allow-mixed to write anyway, or --reset to start over.",
            file=sys.stderr,
        )
        return 1

    rows = simulate(
        before, after, days=args.days, per_day=args.per_day, seed=args.seed, end=datetime.now(UTC)
    )
    conn.executemany(
        "INSERT INTO attempts (id, ts, agent, merchant_name, merchant_url, total_amount,"
        " currency, verdict, rule_id, reason, session_id, payment_url, merchant_country,"
        " products, base_amount, answered_at, answer_note, source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()

    mix: dict[str, int] = {}
    for row in rows:
        mix[row[8]] = mix.get(row[8], 0) + 1
    print(f"\n{BOLD}wrote {len(rows)} simulated attempts to {path}{RESET}")
    print(f"  {DIM}{args.days} days, seed {args.seed}, policy {args.policy}{RESET}\n")
    for rule, count in sorted(mix.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>5}  {rule}")
    print(f"\n  {DIM}policy written to {written}{RESET}")
    print(
        f"  {BOLD}PAY_WARDEN_DB={path} PAY_WARDEN_POLICY={written}"
        f" python -m pay_warden.dashboard{RESET}\n"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
