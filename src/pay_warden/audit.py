"""SQLite audit store: every purchase attempt is recorded, allowed or not.

Also the source of truth the policy engine reads for daily-budget and
velocity checks (only ALLOWED attempts count as spend).
"""

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
    payment_url TEXT
);
"""


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record(
        self,
        req: PurchaseRequest,
        decision: Decision,
        session_id: str | None = None,
        payment_url: str | None = None,
    ) -> str:
        attempt_id = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
        self._conn.execute(
            "UPDATE attempts SET verdict = ?, session_id = ?, payment_url = ? WHERE id = ?",
            (Verdict.ALLOWED.value, session_id, payment_url, attempt_id),
        )
        self._conn.commit()

    def spent_today(self, agent: str) -> Decimal:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self._conn.execute(
            "SELECT total_amount FROM attempts WHERE agent = ? AND verdict = ? AND ts >= ?",
            (agent, Verdict.ALLOWED.value, midnight.isoformat()),
        ).fetchall()
        return sum((Decimal(r["total_amount"]) for r in rows), Decimal("0"))

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
