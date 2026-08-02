"""What the audit trail can honestly be asked, and what it cannot.

Every number the operator dashboard shows is defined here, in one place, so that
a figure on the page can be traced to the rows it came from. The dashboard is a
thin adapter over `build()`; nothing in the renderer derives a statistic of its
own.

Three rules hold the money arithmetic up, and each exists because of a specific
way this schema can mislead:

**A — money is summed in Python with Decimal, never by SQLite.** The amount
columns are TEXT, and `SUM()` coerces TEXT to REAL, so an aggregate over money
would be float. This project already fixed one currency bug; a float one would
be the same mistake wearing a different hat.

**B — a money aggregate includes only convertible rows.** `server.py` writes
`base_amount = to_base(...) or total_amount`, and `to_base` returns None when no
rate is known. So a row in an unpriced currency carries a *raw foreign amount* in
a column named `base_amount`, and summing it blends currencies silently. Every
total here filters to rows whose currency the policy can convert, and reports the
excluded count beside itself rather than hiding it.

**C — two denominators, never mixed.** `total_amount` per currency is the volume
scoreboard. `base_amount` is used only for policy-relative figures, because that
is precisely the number the engine compared against a limit.

And one rule about language. A minted session means a payment page was created,
never that money moved — nothing polls Prava back. So the word here is
*authorised*, never GMV, revenue, or processed.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pay_warden.models import Verdict
from pay_warden.policy import Policy, host_of

# The rules the engine can emit, each annotated with where it is raised. Mirrored
# rather than imported because `rule_id` is a free TEXT column and the
# second-opinion hook (server.py:26-38) may return any string: anything not on
# this list is bucketed, never dropped and never crashed on.
KNOWN_RULES = (
    "unknown-agent",  # policy.py:99
    "total-mismatch",  # :103
    "currency",  # :108
    "unknown-rate",  # :113
    "merchant-deny",  # :121
    "merchant-allow",  # :125
    "max-single-purchase",  # :129
    "daily-budget",  # :137
    "velocity",  # :144
    "human-approval",  # :152
    "pass",  # :158
)

# Every rule evaluated *before* the human-approval threshold. Used to define the
# what-if population by exclusion: a row stopped by one of these never reached
# the threshold, so moving the threshold could not have changed it. Exclusion
# rather than inclusion so an unknown rule_id lands in the population it belongs
# to instead of being silently dropped.
_RULES_BEFORE_APPROVAL = KNOWN_RULES[:9]

ESCALATION_RULE = "human-approval"
ADAPTATION_WINDOW_MINUTES = 60
# Rules an agent could plausibly respond to by asking for something smaller.
_ADAPTABLE = ("max-single-purchase", "daily-budget", "velocity")


# --- capability probing -------------------------------------------------------


def capabilities(conn: sqlite3.Connection) -> dict[str, bool]:
    """Which optional columns this database has.

    The dashboard opens the audit DB read-only, so it cannot migrate one. That
    is deliberate: it can be pointed at a colleague's older file and report what
    is missing instead of quietly rewriting it.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
    return {
        "released_at": "released_at" in columns,
        "source": "source" in columns,
    }


# --- money, under rules A and B -----------------------------------------------


def _decimal(raw: object) -> Decimal | None:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None


def _sum_base(rows: list[sqlite3.Row], policy: Policy) -> tuple[Decimal, int]:
    """Decimal-sum `base_amount`, excluding rows whose currency has no rate.

    Returns the total and the number of rows left out. Excluding is the
    conservative direction: a row whose currency the policy cannot price carries
    an amount that is not in the base currency at all, and including it would
    overstate or understate by whatever the rate happens to be.
    """
    total = Decimal("0")
    unpriceable = 0
    for row in rows:
        amount = _decimal(row["base_amount"])
        if amount is None or row["currency"] not in policy.rates:
            unpriceable += 1
            continue
        total += amount
    return total, unpriceable


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


# --- provenance ---------------------------------------------------------------


def _provenance(
    conn: sqlite3.Connection, caps: dict[str, bool], where: str, params: tuple
) -> dict[str, Any]:
    """How many of the rows behind *this* panel were simulated.

    Per panel rather than per page: the merchant-readiness panel reads a real
    registry even when the audit rows beside it are synthetic, and a single
    page-level flag would erase that distinction. The renderer puts the label in
    every panel heading, so a crop that loses the label loses the number too.
    """
    clause = f"WHERE {where}" if where else ""
    if not caps["source"]:
        (total,) = conn.execute(f"SELECT COUNT(*) FROM attempts {clause}", params).fetchone()
        return {"simulated": 0, "total": total, "label": ""}
    row = conn.execute(
        f"SELECT COUNT(*) AS total,"
        f" SUM(CASE WHEN source = 'simulated' THEN 1 ELSE 0 END) AS simulated"
        f" FROM attempts {clause}",
        params,
    ).fetchone()
    simulated = row["simulated"] or 0
    total = row["total"] or 0
    label = "SIMULATED" if simulated == total and total else ("MIXED" if simulated else "")
    return {"simulated": simulated, "total": total, "label": label}


# --- the panels ---------------------------------------------------------------


def _totals(conn: sqlite3.Connection, policy: Policy, since: str) -> dict[str, Any]:
    """Counts over the whole window, never over the feed.

    The tiles used to be computed from `recent(limit=200)` and rendered as
    totals, which is true only while the table is smaller than the page.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS attempts,"
        " SUM(CASE WHEN verdict = 'allowed' THEN 1 ELSE 0 END) AS allowed,"
        " SUM(CASE WHEN verdict = 'denied' THEN 1 ELSE 0 END) AS denied,"
        " SUM(CASE WHEN verdict = 'needs_approval' THEN 1 ELSE 0 END) AS needs_approval,"
        " SUM(CASE WHEN session_id IS NOT NULL AND session_id <> '' THEN 1 ELSE 0 END) AS sessions"
        " FROM attempts WHERE ts >= ?",
        (since,),
    ).fetchone()

    # Value authorised, per currency and never blended: there is no stored FX
    # rate, so a single cross-currency total would be a fiction.
    authorised: dict[str, Decimal] = {}
    for minted in conn.execute(
        "SELECT total_amount, currency FROM attempts"
        " WHERE ts >= ? AND session_id IS NOT NULL AND session_id <> ''",
        (since,),
    ):
        amount = _decimal(minted["total_amount"])
        if amount is None:
            continue
        currency = minted["currency"]
        authorised[currency] = authorised.get(currency, Decimal("0")) + amount

    return {
        "attempts": row["attempts"] or 0,
        "allowed": row["allowed"] or 0,
        "denied": row["denied"] or 0,
        "needs_approval": row["needs_approval"] or 0,
        "sessions": row["sessions"] or 0,
        "authorised": {code: _money(value) for code, value in sorted(authorised.items())},
    }


def _escalation(
    conn: sqlite3.Connection, policy: Policy, caps: dict[str, bool], since: str, now: datetime
) -> dict[str, Any]:
    """The flagship: what happens at the moment a limit bites.

    `mark_released` leaves `rule_id` intact, so (allowed, human-approval) is the
    fingerprint of "escalated then released" and (needs_approval, human-approval)
    is one still waiting.
    """
    counts = conn.execute(
        "SELECT SUM(CASE WHEN verdict = 'needs_approval' THEN 1 ELSE 0 END) AS pending,"
        " SUM(CASE WHEN verdict = 'allowed' THEN 1 ELSE 0 END) AS released"
        " FROM attempts WHERE rule_id = ? AND ts >= ?",
        (ESCALATION_RULE, since),
    ).fetchone()
    pending = counts["pending"] or 0
    released = counts["released"] or 0
    raised = pending + released

    friction_rows = conn.execute(
        "SELECT base_amount, currency FROM attempts"
        " WHERE rule_id = ? AND verdict = 'allowed' AND ts >= ?",
        (ESCALATION_RULE, since),
    ).fetchall()
    friction, friction_unpriceable = _sum_base(friction_rows, policy)

    # A stock, not a flow, so deliberately unwindowed: what is waiting right now.
    held_rows = conn.execute(
        "SELECT base_amount, currency FROM attempts WHERE verdict = 'needs_approval'"
    ).fetchall()
    held, held_unpriceable = _sum_base(held_rows, policy)

    oldest = conn.execute(
        "SELECT MIN(ts) FROM attempts WHERE verdict = 'needs_approval'"
    ).fetchone()[0]
    oldest_wait_s = None
    if oldest and (parsed := _parse(oldest)) is not None:
        oldest_wait_s = max(0, int((now - parsed).total_seconds()))

    load = [
        {"day": row["day"], "n": row["n"]}
        for row in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM attempts"
            " WHERE rule_id = ? AND ts >= ? GROUP BY day ORDER BY day",
            (ESCALATION_RULE, since),
        )
    ]

    return {
        "raised": raised,
        "released": released,
        "pending": pending,
        # A lower bound, not an approval rate: pay-warden has no rejection
        # state, so a human who decided no leaves a row identical to one nobody
        # has looked at yet.
        "release_rate": (released / raised) if raised else None,
        "friction_cost": _money(friction),
        "value_held_now": _money(held),
        "unpriceable_rows": friction_unpriceable + held_unpriceable,
        "oldest_pending_s": oldest_wait_s,
        "latency": _latency(conn, caps, since),
        "load_by_day": load,
        "provenance": _provenance(conn, caps, "rule_id = ? AND ts >= ?", (ESCALATION_RULE, since)),
    }


def _parse(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _latency(conn: sqlite3.Connection, caps: dict[str, bool], since: str) -> dict[str, Any]:
    """How long a human took, for the releases that were instrumented.

    Never a bare median. `released_at` was added after the fact and cannot be
    backfilled, so every figure travels with `n` and the untimed count — a
    median over an unstated population is a lie by omission.
    """
    if not caps["released_at"]:
        return {"available": False, "reason": "this database predates released_at"}

    waits: list[int] = []
    untimed = 0
    invalid = 0
    for row in conn.execute(
        "SELECT ts, released_at FROM attempts"
        " WHERE rule_id = ? AND verdict = 'allowed' AND ts >= ?",
        (ESCALATION_RULE, since),
    ):
        if not row["released_at"]:
            untimed += 1
            continue
        parked, answered = _parse(row["ts"]), _parse(row["released_at"])
        if parked is None or answered is None:
            invalid += 1
            continue
        seconds = int((answered - parked).total_seconds())
        # A negative wait is a clock or generator bug, never a fact. Discard and
        # count it: letting one into a median drags the number down invisibly.
        if seconds < 0:
            invalid += 1
            continue
        waits.append(seconds)
    waits.sort()
    return {
        "available": True,
        "n": len(waits),
        "untimed": untimed,
        "invalid": invalid,
        "median_s": int(statistics.median(waits)) if waits else None,
        "p90_s": waits[min(len(waits) - 1, int(len(waits) * 0.9))] if waits else None,
        "max_s": waits[-1] if waits else None,
    }


def _threshold_whatif(
    conn: sqlite3.Connection, policy: Policy, caps: dict[str, bool], since: str
) -> dict[str, Any]:
    """Where the escalation threshold could sit, replayed against real rows.

    Not a model. `human_approval_over` is the last gate, so every row that
    reached it either passed or escalated; re-running the comparison at
    `policy.py:149` against those exact amounts is a replay, and the only
    assumption is that the same requests would have been made.
    """
    placeholders = ",".join("?" * len(_RULES_BEFORE_APPROVAL))
    rows = conn.execute(
        f"SELECT base_amount, currency FROM attempts"
        f" WHERE ts >= ? AND rule_id NOT IN ({placeholders})",
        (since, *_RULES_BEFORE_APPROVAL),
    ).fetchall()

    amounts = [
        amount
        for row in rows
        if row["currency"] in policy.rates and (amount := _decimal(row["base_amount"])) is not None
    ]
    current = policy.human_approval_over
    if not amounts:
        return {
            "population": 0,
            "current": _money(current) if current is not None else None,
            "curve": [],
            "provenance": _provenance(conn, caps, "ts >= ?", (since,)),
        }

    curve = []
    for threshold in _ladder(min(amounts), max(amounts), current):
        over = [a for a in amounts if a > threshold]
        curve.append(
            {
                "threshold": _money(threshold),
                "escalations": len(over),
                "value_held": _money(sum(over, Decimal("0"))),
                "share": len(over) / len(amounts),
                "current": current is not None and threshold == current,
            }
        )
    return {
        "population": len(amounts),
        "current": _money(current) if current is not None else None,
        "curve": curve,
        "unpriceable_rows": len(rows) - len(amounts),
        "provenance": _provenance(
            conn,
            caps,
            f"ts >= ? AND rule_id NOT IN ({placeholders})",
            (since, *_RULES_BEFORE_APPROVAL),
        ),
    }


def _ladder(low: Decimal, high: Decimal, current: Decimal | None) -> list[Decimal]:
    """Round candidate thresholds spanning the observed range.

    A 1-2-5 ladder rather than even steps, because the reader is choosing a
    number to ship and will ship a round one. The current threshold is forced in
    so the page always shows where it stands today.
    """
    candidates: set[Decimal] = set()
    step = Decimal("1")
    while step <= max(high, Decimal("1")):
        for multiple in (Decimal("1"), Decimal("2"), Decimal("5")):
            value = step * multiple
            if low <= value <= high:
                candidates.add(value)
        step *= 10
    if current is not None:
        candidates.add(current)
    return sorted(candidates)[:12]


def _rules(
    conn: sqlite3.Connection, policy: Policy, caps: dict[str, bool], since: str
) -> dict[str, Any]:
    """Denial pressure, worded so the counts cannot be misread.

    `Policy.evaluate` is first-match-wins, so these are requests each rule
    *stopped*, not requests that violated it. A request both over-cap and at a
    denied merchant appears only under `merchant-deny`.
    """
    denials = []
    for row in conn.execute(
        "SELECT rule_id, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
        " FROM attempts WHERE verdict = 'denied' AND ts >= ?"
        " GROUP BY rule_id ORDER BY n DESC",
        (since,),
    ):
        amounts = conn.execute(
            "SELECT base_amount, currency FROM attempts"
            " WHERE verdict = 'denied' AND rule_id = ? AND ts >= ?",
            (row["rule_id"], since),
        ).fetchall()
        value, unpriceable = _sum_base(amounts, policy)
        denials.append(
            {
                "rule_id": row["rule_id"],
                "known": row["rule_id"] in KNOWN_RULES,
                "n": row["n"],
                "value": _money(value),
                "unpriceable_rows": unpriceable,
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
            }
        )
    total = sum(d["n"] for d in denials) or 1
    for denial in denials:
        denial["share"] = denial["n"] / total

    return {
        "stopped_by": denials,
        "outside_vocabulary": [d for d in denials if not d["known"]],
        "adaptation": _adaptation(conn, since),
        "provenance": _provenance(conn, caps, "verdict = 'denied' AND ts >= ?", (since,)),
    }


def _adaptation(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    """Blocked attempts followed by a smaller successful one, same merchant.

    The nearest thing to a counterfactual this trail supports, and it is
    reported as a count of a defined cohort rather than a rate. It does not
    prove causation — the constraints make coincidence unlikely, and that is all.
    """
    placeholders = ",".join("?" * len(_ADAPTABLE))
    denied = conn.execute(
        f"SELECT ts, agent, merchant_url, base_amount FROM attempts"
        f" WHERE verdict = 'denied' AND rule_id IN ({placeholders}) AND ts >= ? ORDER BY ts",
        (*_ADAPTABLE, since),
    ).fetchall()
    allowed: dict[tuple[str, str], list[tuple[datetime, Decimal]]] = {}
    for row in conn.execute(
        "SELECT ts, agent, merchant_url, base_amount FROM attempts"
        " WHERE verdict = 'allowed' AND ts >= ? ORDER BY ts",
        (since,),
    ):
        when, amount = _parse(row["ts"]), _decimal(row["base_amount"])
        if when is None or amount is None:
            continue
        allowed.setdefault((row["agent"], host_of(row["merchant_url"])), []).append((when, amount))

    window = timedelta(minutes=ADAPTATION_WINDOW_MINUTES)
    followed = 0
    for row in denied:
        when, amount = _parse(row["ts"]), _decimal(row["base_amount"])
        if when is None or amount is None:
            continue
        key = (row["agent"], host_of(row["merchant_url"]))
        if any(
            when < later <= when + window and smaller < amount
            for later, smaller in allowed.get(key, ())
        ):
            followed += 1
    return {
        "denied": len(denied),
        "followed_by_smaller_allowed": followed,
        "window_minutes": ADAPTATION_WINDOW_MINUTES,
    }


def _activation(
    conn: sqlite3.Connection, policy: Policy, caps: dict[str, bool]
) -> dict[str, Any]:
    """Identities pay-warden refused because it had never heard of them.

    Classification is a lifetime property, so this is deliberately unwindowed.
    An `unknown-agent` denial is either an integration that never registered or
    a caller correctly refused, and this trail cannot tell them apart — which is
    why the panel is headed "unregistered identities", not "failed integrations".
    """
    identities = []
    for row in conn.execute(
        "SELECT agent,"
        " SUM(CASE WHEN rule_id = 'unknown-agent' THEN 1 ELSE 0 END) AS unknown_n,"
        " SUM(CASE WHEN rule_id <> 'unknown-agent' THEN 1 ELSE 0 END) AS known_n,"
        " MIN(CASE WHEN rule_id = 'unknown-agent' THEN ts END) AS first_unknown,"
        " MIN(CASE WHEN rule_id <> 'unknown-agent' THEN ts END) AS first_known,"
        " COUNT(DISTINCT merchant_url) AS merchants, COUNT(*) AS attempts"
        " FROM attempts GROUP BY agent HAVING unknown_n > 0 ORDER BY unknown_n DESC"
    ):
        first_unknown, first_known = _parse(row["first_unknown"] or ""), _parse(
            row["first_known"] or ""
        )
        recovered = bool(row["known_n"]) and first_known is not None and first_unknown is not None
        identities.append(
            {
                "agent": row["agent"],
                "tenant": _tenant(row["agent"]),
                "unknown_n": row["unknown_n"],
                "attempts": row["attempts"],
                "merchants": row["merchants"],
                "recovered": recovered,
                "seconds_to_registration": (
                    int((first_known - first_unknown).total_seconds())
                    if recovered and first_known > first_unknown
                    else None
                ),
            }
        )

    bounced_rows = conn.execute(
        "SELECT base_amount, currency FROM attempts WHERE rule_id = 'unknown-agent'"
    ).fetchall()
    bounced, unpriceable = _sum_base(bounced_rows, policy)
    times = [i["seconds_to_registration"] for i in identities if i["seconds_to_registration"]]
    return {
        "identities": len(identities),
        "stuck": sum(1 for i in identities if not i["recovered"]),
        "recovered": sum(1 for i in identities if i["recovered"]),
        "denials": sum(i["unknown_n"] for i in identities),
        "value_bounced": _money(bounced),
        "unpriceable_rows": unpriceable,
        "median_seconds_to_registration": int(statistics.median(times)) if times else None,
        "top": identities[:10],
        "provenance": _provenance(conn, caps, "rule_id = 'unknown-agent'", ()),
    }


def _tenant(agent: str) -> str:
    """The prefix an agent names itself with.

    A naming convention read off an opaque string, never enforced tenancy —
    pay-warden does not parse `agent` anywhere else, and anyone can claim any
    prefix. Said on the panel too.
    """
    return agent.split(":", 1)[0] if ":" in agent else "(no prefix)"


def _agents(
    conn: sqlite3.Connection, policy: Policy, now: datetime
) -> list[dict[str, Any]]:
    """Every agent in the trail, not just the ones the policy knows about.

    The previous version iterated the policy file, so an unregistered caller —
    the exact population the activation panel is about — was invisible on the
    page meant to expose it.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cutoff = (now - timedelta(minutes=policy.velocity_window_minutes)).isoformat()

    seen = [row["agent"] for row in conn.execute("SELECT DISTINCT agent FROM attempts")]
    out = []
    for agent in sorted(set(seen) | set(policy.agents)):
        limits = policy.agents.get(agent)
        rows = conn.execute(
            "SELECT base_amount, currency FROM attempts"
            " WHERE agent = ? AND verdict = 'allowed' AND ts >= ?",
            (agent, midnight),
        ).fetchall()
        spent, unpriceable = _sum_base(rows, policy)
        (in_window,) = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE agent = ? AND verdict = 'allowed' AND ts >= ?",
            (agent, cutoff),
        ).fetchone()
        budget = limits.daily_budget if limits else None
        out.append(
            {
                "agent": agent,
                "tenant": _tenant(agent),
                "registered": limits is not None,
                "spent": _money(spent),
                "unpriceable_rows": unpriceable,
                "budget": str(budget) if budget is not None else None,
                "cap": str(limits.max_single_purchase) if limits else None,
                "used": float(spent / budget) if budget else None,
                "in_window": in_window,
                "velocity_max": policy.velocity_max,
            }
        )
    out.sort(key=lambda a: (a["used"] is None, -(a["used"] or 0)))
    return out


def _feed(conn: sqlite3.Connection, caps: dict[str, bool], limit: int) -> dict[str, Any]:
    """The most recent N rows, labelled as such.

    Named `feed` rather than `attempts` so nothing downstream can mistake it for
    a population. Every aggregate above is computed in SQL over the whole
    window; none of them is derived from these rows.
    """
    rows = [
        dict(r)
        for r in conn.execute("SELECT * FROM attempts ORDER BY ts DESC LIMIT ?", (limit,))
    ]
    (total,) = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()
    return {"rows": rows, "limit": limit, "total": total}


LIMITS = (
    {
        "text": "No settlement confirmation — a minted session means a payment page existed,"
        " never that money moved.",
        "fix": "Ingest Prava's session webhooks.",
    },
    {
        "text": "No preview traffic — preview_purchase records nothing, so denial counts are a"
        " floor rather than a total.",
        "fix": "Record previews behind a flag.",
    },
    {
        "text": "No policy version on a row, so a trend crossing a policy edit is not"
        " like-for-like.",
        "fix": "Stamp a policy hash at decision time.",
    },
    {
        "text": "No decline record — a human who refuses an escalation leaves no trace, so"
        " release rate is a lower bound.",
        "fix": "Add a rejected verdict and a reject_purchase tool.",
    },
    {
        "text": "Time-to-release starts at instrumentation; releases recorded before that column"
        " existed are excluded from every latency figure.",
        "fix": "None — the gap is historical and cannot be recovered.",
    },
)


def attempted_hosts(conn: sqlite3.Connection, since: str) -> dict[str, dict[str, Any]]:
    """Merchants agents actually tried, keyed by the host the engine matched on.

    This is pay-warden's half of the readiness join: canibuy can rank merchants
    by how many exist, only this trail can rank them by money someone tried to
    spend there.
    """
    hosts: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT merchant_url, COUNT(*) AS n FROM attempts WHERE ts >= ? GROUP BY merchant_url",
        (since,),
    ):
        host = host_of(row["merchant_url"])
        if not host:
            continue
        entry = hosts.setdefault(host, {"attempts": 0})
        entry["attempts"] += row["n"]
    return hosts


def build(
    conn: sqlite3.Connection,
    policy: Policy,
    *,
    now: datetime,
    window_days: int = 30,
    feed_limit: int = 200,
    merchant_reader: Any = None,
) -> dict[str, Any]:
    """Everything the operator page renders, computed once.

    `now` is injected rather than read from the clock. Every window here — the
    last 30 days, today's spend, the age of the oldest pending escalation —
    depends on it, and a metric layer that reads the clock itself cannot be
    tested at a fixed point.

    `merchant_reader` is passed the attempted-host map and returns the readiness
    panel. Injected rather than imported so this module has no opinion about
    where merchant grades come from, and so the tests never need one.
    """
    caps = capabilities(conn)
    since = (now - timedelta(days=window_days)).isoformat()
    merchants = (
        merchant_reader(attempted_hosts(conn, since))
        if merchant_reader is not None
        else {"available": False, "reason": "not configured"}
    )
    if merchants.get("available"):
        # The registry is real, but the attempt counts joined onto it come from
        # this trail. The panel is only as real as its least real half, so it
        # takes the audit provenance — and the panel says which half is which.
        merchants["provenance"] = _provenance(conn, caps, "ts >= ?", (since,))

    return {
        "meta": {
            "generated_at": now.isoformat(),
            "window_days": window_days,
            "window_from": since,
            "base_currency": policy.base_currency,
            "capabilities": caps,
            "provenance": _provenance(conn, caps, "", ()),
        },
        "totals": _totals(conn, policy, since),
        "escalation": _escalation(conn, policy, caps, since, now),
        "threshold": _threshold_whatif(conn, policy, caps, since),
        "rules": _rules(conn, policy, caps, since),
        "activation": _activation(conn, policy, caps),
        "agents": _agents(conn, policy, now),
        "merchants": merchants,
        "feed": _feed(conn, caps, feed_limit),
        "limits": list(LIMITS),
    }
