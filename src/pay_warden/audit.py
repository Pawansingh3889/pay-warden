"""SQLite audit store: every purchase attempt is recorded, allowed or not.

Also the source of truth the policy engine reads for daily-budget and
velocity checks (only ALLOWED attempts count as spend).
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pay_warden.models import Decision, PurchaseRequest, Verdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    merchant_name TEXT NOT NULL,
    merchant_url TEXT NOT NULL,
    total_amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    verdict TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    session_id TEXT,
    payment_url TEXT,
    merchant_country TEXT NOT NULL DEFAULT '',
    products TEXT NOT NULL DEFAULT '[]',
    base_amount TEXT NOT NULL DEFAULT '',
    answered_at TEXT NOT NULL DEFAULT '',
    answer_note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'live'
);
"""

# Columns added after the first schema shipped; older DBs get them via ALTER.
_MIGRATIONS = {
    "merchant_country": "ALTER TABLE attempts ADD COLUMN merchant_country TEXT NOT NULL DEFAULT ''",
    "products": "ALTER TABLE attempts ADD COLUMN products TEXT NOT NULL DEFAULT '[]'",
    "base_amount": "ALTER TABLE attempts ADD COLUMN base_amount TEXT NOT NULL DEFAULT ''",
    # When a human answered a parked attempt, either way. `ts` stays the moment
    # the limit fired; this is the moment somebody decided, and the gap between
    # them is the only measure of whether a threshold is friction or protection.
    #
    # It was `released_at` for exactly as long as releasing was the only answer
    # a person could give. Adding a rejection made that name a half-truth, and a
    # column that is right about one branch is worse than one that is right
    # about both. Backfilled below, because a release genuinely is an answer.
    "answered_at": "ALTER TABLE attempts ADD COLUMN answered_at TEXT NOT NULL DEFAULT ''",
    # What the person said when they refused, in their own words. Kept apart
    # from `reason`, which holds the policy engine's wording and must survive
    # intact — the whole product rests on a denial being relayed as the rule
    # actually worded it, not as somebody later summarised it.
    "answer_note": "ALTER TABLE attempts ADD COLUMN answer_note TEXT NOT NULL DEFAULT ''",
    # Where the row came from. Per row rather than per file, because the
    # realistic state of a demo database is mixed: a real run, then a simulated
    # sweep, into the same file.
    "source": "ALTER TABLE attempts ADD COLUMN source TEXT NOT NULL DEFAULT 'live'",
}

# Both cover the engine's hot path — spent_today and purchases_in_window run on
# every purchase decision, and both were full scans. Any reporting built on this
# table is the secondary beneficiary, not the justification.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_agent_verdict_ts ON attempts(agent, verdict, ts)",
)


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(attempts)")}
        for column, ddl in _MIGRATIONS.items():
            if column not in existing:
                self._conn.execute(ddl)
        # Rows written before base_amount existed were single-currency by
        # assumption; backfill from total_amount so spend history survives the
        # migration instead of silently dropping to zero.
        self._conn.execute(
            "UPDATE attempts SET base_amount = total_amount WHERE base_amount = ''"
        )
        # `answered_at` was `released_at` while releasing was the only answer.
        # Copying one into the other is legitimate for the same reason the line
        # above is: a release *is* an answer, so the old value is correct in the
        # new column. Guarded on the old column still existing, since a database
        # created after the rename never had one.
        if "released_at" in existing and "answered_at" not in existing:
            self._conn.execute("UPDATE attempts SET answered_at = released_at")
        # What is still deliberately NOT backfilled is `answered_at` from `ts`.
        # total_amount was a correct substitute for a single-currency row; there
        # is no correct substitute for a timestamp nobody recorded. Taking `ts`
        # would claim every historical answer was instant, and an invented
        # latency is worse than an admitted gap — so the series starts at
        # instrumentation, and the dashboard says so on every latency figure.
        for ddl in _INDEXES:
            self._conn.execute(ddl)
        self._conn.commit()

    def record(
        self,
        req: PurchaseRequest,
        decision: Decision,
        session_id: str | None = None,
        payment_url: str | None = None,
        base_amount: Decimal | None = None,
    ) -> str:
        """Record an attempt.

        `base_amount` is the total converted to the policy's base currency and is
        what budgets are enforced against. Omit it only when the request is
        already in the base currency.
        """
        attempt_id = uuid.uuid4().hex[:12]
        products = json.dumps(
            [
                {"description": p.description, "unit_price": str(p.unit_price), "quantity": p.quantity}
                for p in req.products
            ]
        )
        self._conn.execute(
            "INSERT INTO attempts (id, ts, agent, merchant_name, merchant_url, total_amount,"
            " currency, verdict, rule_id, reason, session_id, payment_url, merchant_country,"
            " products, base_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                datetime.now(UTC).isoformat(),
                req.agent,
                req.merchant_name,
                req.merchant_url,
                str(req.total_amount),
                req.currency,
                decision.verdict.value,
                decision.rule_id,
                decision.reason,
                session_id,
                payment_url,
                req.merchant_country,
                products,
                str(base_amount if base_amount is not None else req.total_amount),
            ),
        )
        self._conn.commit()
        return attempt_id

    def get(self, attempt_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row else None

    def mark_released(self, attempt_id: str, session_id: str, payment_url: str) -> None:
        """Flip a parked attempt to allowed, and record when.

        `ts` is deliberately left alone. It is when the limit fired; `answered_at`
        is when a human answered. Rewriting `ts` would erase the wait, which is
        the one thing this pair of columns exists to measure.
        """
        self._conn.execute(
            "UPDATE attempts SET verdict = ?, session_id = ?, payment_url = ?, answered_at = ?"
            " WHERE id = ? AND verdict = ?",
            (
                Verdict.ALLOWED.value,
                session_id,
                payment_url,
                datetime.now(UTC).isoformat(),
                attempt_id,
                Verdict.NEEDS_APPROVAL.value,
            ),
        )
        self._conn.commit()

    def mark_rejected(self, attempt_id: str, note: str = "") -> bool:
        """Record that a person refused a parked attempt.

        Terminal, and it mints nothing: the whole point is that no money moved
        and none can later, because the row is no longer `needs_approval` and
        `approve_purchase` only acts on one that is.

        The `verdict = needs_approval` guard is in the WHERE clause rather than
        a read-then-write, so a refusal racing an approval cannot both succeed.
        Returns whether this call was the one that decided it.
        """
        cursor = self._conn.execute(
            "UPDATE attempts SET verdict = ?, answered_at = ?, answer_note = ?"
            " WHERE id = ? AND verdict = ?",
            (
                Verdict.REJECTED.value,
                datetime.now(UTC).isoformat(),
                note,
                attempt_id,
                Verdict.NEEDS_APPROVAL.value,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def spent_today(self, agent: str) -> Decimal:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self._conn.execute(
            "SELECT base_amount FROM attempts WHERE agent = ? AND verdict = ? AND ts >= ?",
            (agent, Verdict.ALLOWED.value, midnight.isoformat()),
        ).fetchall()
        return sum((Decimal(r["base_amount"]) for r in rows), Decimal("0"))

    def purchases_in_window(self, agent: str, window_minutes: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
        (count,) = self._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE agent = ? AND verdict = ? AND ts >= ?",
            (agent, Verdict.ALLOWED.value, cutoff.isoformat()),
        ).fetchone()
        return count

    def recent(self, agent: str | None = None, limit: int = 20) -> list[dict]:
        if agent:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE agent = ? ORDER BY ts DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM attempts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
