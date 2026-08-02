"""A read-only window onto canibuy's merchant registry.

canibuy grades merchants for agent-readiness. pay-warden knows which merchants
agents actually tried to spend money at. Neither number is interesting alone;
together they say *which unreachable merchants are costing you real attempts*,
which is the only version of "improve merchant coverage" that has a work queue
attached to it.

**This is a link, not a dependency.** canibuy is not imported: it pins
`mcp>=1.2` against this project's `mcp>=1.2`-compatible-but-diverging tree, and a
reporting panel is not worth a resolver conflict. The two queries below mirror
`Registry.all_latest()` and `Registry.drift_all()`, each annotated with the
function it mirrors, and the connection is opened `mode=ro` so this process can
never write to another product's database.

Every failure is a sentence, never an exception. A dashboard that 500s because a
sibling project's file moved is worse than one that says the file moved.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from pay_warden.policy import host_of

ENV_VARS = ("PAY_WARDEN_CANIBUY_DB", "CANIBUY_DB")
STALE_AFTER_HOURS = 24 * 7

# canibuy's grades, best first. `BUYABLE` mirrors canibuy/service.py:15.
GRADES = ("A+", "A", "B", "C", "D", "F")
BUYABLE = ("A+", "A", "B")

# Mirrored from canibuy/models.py:88-153, which is the source of truth. Kept as
# plain data so this module needs no canibuy import; an env-gated test compares
# the two so the copy cannot rot unnoticed. The {owner, fix, unlockable} triple
# is what turns a grade into a work queue — without an owner, a failing merchant
# is a complaint rather than a task.
_REMEDIATION: dict[str, dict[str, Any]] = {
    "unreachable": {"owner": "merchant", "unlockable": True,
                    "fix": "site did not respond — check uptime and DNS"},
    "robots-disallowed": {"owner": "merchant", "unlockable": True,
                          "fix": "robots.txt forbids automated visits; permit agent traffic"},
    "bot-wall": {"owner": "merchant", "unlockable": True,
                 "fix": "bot defences block honest agent traffic at the door; allowlist agent"
                        " user-agents on product and checkout paths"},
    "no-products": {"owner": "merchant", "unlockable": True,
                    "fix": "no machine-readable catalogue; publish schema.org/Product JSON-LD"},
    "login-wall": {"owner": "merchant", "unlockable": True,
                   "fix": "checkout requires an account; enable guest checkout"},
    "no-guest-path": {"owner": "merchant", "unlockable": True,
                      "fix": "no reachable checkout URL found; expose a linkable cart route"},
    "card-form-in-cross-origin-iframe": {"owner": "nobody", "unlockable": False,
                                         "fix": "card fields live in a PSP iframe — normal and"
                                                " automatable, just slower"},
    "issuer-challenge": {"owner": "issuer", "unlockable": False,
                         "fix": "3DS/OTP is issuer-side; expect a human tap"},
    "price-drift-vs-mandate": {"owner": "agent", "unlockable": True,
                               "fix": "displayed price moved away from the mandate; re-price"
                                      " before authorising"},
    "currency-mismatch": {"owner": "agent", "unlockable": True,
                          "fix": "settle in the merchant's currency or convert before minting"},
    "session-rejected": {"owner": "agent", "unlockable": True,
                         "fix": "Prava refused the session — check amount, currency and merchant"},
    "payment-failed": {"owner": "agent", "unlockable": True,
                       "fix": "payment did not complete; inspect the transaction error"},
}


def _path() -> str:
    for name in ENV_VARS:
        if value := os.environ.get(name, "").strip():
            return value
    return ""


def _unavailable(reason: str, how: str = "") -> dict[str, Any]:
    return {"available": False, "reason": reason, "how": how}


def _connect(path: str) -> sqlite3.Connection:
    # timeout is short on purpose: canibuy does not use WAL, so a probe sweep
    # holding an exclusive lock would otherwise stall this request behind it.
    # A late panel is worse than an honest "database is locked".
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    return conn


def _failure_classes(stages_json: str) -> set[str]:
    """The failure classes a probe recorded, read out of its stages blob."""
    try:
        stages = json.loads(stages_json or "[]")
    except (ValueError, TypeError):
        return set()
    if not isinstance(stages, list):
        return set()
    return {
        str(stage["failure_class"])
        for stage in stages
        if isinstance(stage, dict) and stage.get("failure_class")
    }


def read(attempted: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The readiness panel, joined to what agents actually attempted.

    `attempted` maps a registrable host to `{"attempts": int, "value": Decimal}`
    as computed from the audit trail. Passing None gives the registry on its own.
    """
    path = _path()
    if not path:
        return _unavailable(
            "not configured",
            f"set {ENV_VARS[0]} to a canibuy registry to join grades to your own traffic",
        )
    if not os.path.exists(path):
        return _unavailable(f"no registry at {path}")

    conn = None
    try:
        conn = _connect(path)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "probes" not in tables or "merchants" not in tables:
            return _unavailable(f"{path} is not a canibuy registry (no probes table)")
        return _build(conn, path, attempted or {})
    except sqlite3.Error as exc:
        # Including "database is locked". A sibling project's file moving or
        # being mid-write must never take this page down.
        return _unavailable(f"could not read {path}: {exc}")
    finally:
        if conn is not None:
            conn.close()


def _build(conn: sqlite3.Connection, path: str, attempted: dict[str, dict]) -> dict[str, Any]:
    # mirrors canibuy Registry.all_latest() (registry.py:220) — newest probe per
    # merchant. Note it inner-joins probes, so a merchant that has never been
    # probed is absent; both denominators are reported below because otherwise
    # this page silently disagrees with canibuy's own.
    latest = conn.execute(
        "SELECT m.url, m.name, p.grade, p.automation_hostile, p.route, p.ts, p.stages"
        " FROM probes p JOIN merchants m ON m.id = p.merchant_id"
        " WHERE p.id = (SELECT MAX(id) FROM probes WHERE merchant_id = m.id)"
    ).fetchall()

    (in_registry,) = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()
    probed_at = conn.execute("SELECT MAX(ts) FROM probes").fetchone()[0]

    by_grade: dict[str, dict[str, Any]] = {
        grade: {"grade": grade, "merchants": 0, "attempts": 0} for grade in GRADES
    }
    work: dict[str, dict[str, Any]] = {}
    matched = 0
    graded_hosts = set()

    for row in latest:
        host = host_of(row["url"])
        graded_hosts.add(host)
        grade = row["grade"] if row["grade"] in by_grade else "F"
        by_grade[grade]["merchants"] += 1

        seen = attempted.get(host)
        if seen:
            matched += 1
            by_grade[grade]["attempts"] += seen["attempts"]

        if grade in BUYABLE:
            continue
        for failure in _failure_classes(row["stages"]):
            known = _REMEDIATION.get(failure)
            entry = work.setdefault(
                failure,
                {
                    "failure_class": failure,
                    "owner": known["owner"] if known else "unknown",
                    "fix": known["fix"] if known else "not in the remediation table",
                    "unlockable": bool(known["unlockable"]) if known else False,
                    "merchants_blocked": 0,
                    "attempts_blocked": 0,
                },
            )
            entry["merchants_blocked"] += 1
            entry["attempts_blocked"] += seen["attempts"] if seen else 0

    # mirrors canibuy Registry.drift_all() (registry.py:254) — the last two
    # probes per merchant, where they disagree. A grade is a probe result at a
    # point in time, not a contract, and this registry has moved a merchant from
    # C to F inside a day.
    drift = []
    seen_url: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT m.url, p.grade, p.ts FROM probes p JOIN merchants m ON m.id = p.merchant_id"
        " ORDER BY p.merchant_id, p.id DESC"
    ):
        seen_url.setdefault(row["url"], []).append(row)
    for url, probes in seen_url.items():
        if len(probes) >= 2 and probes[0]["grade"] != probes[1]["grade"]:
            drift.append(
                {
                    "url": url,
                    "from_grade": probes[1]["grade"],
                    "to_grade": probes[0]["grade"],
                    "to_ts": probes[0]["ts"],
                }
            )

    graded = len(latest)
    buyable = sum(by_grade[g]["merchants"] for g in BUYABLE)
    return {
        "available": True,
        "db": path,
        "probed_at": probed_at,
        "stale_after_hours": STALE_AFTER_HOURS,
        # Both denominators, or this page disagrees with canibuy's and nobody
        # can say why: all_latest() inner-joins probes.
        "registry": {"merchants": in_registry, "graded": graded},
        "coverage": {
            "attempted_hosts": len(attempted),
            "matched": matched,
            "unmatched": len(attempted) - matched,
        },
        "by_grade": [row for row in by_grade.values() if row["merchants"]],
        "buyable_share": (buyable / graded) if graded else None,
        # A merchant blocked by two causes appears under both, so this column
        # does not sum to merchants_blocked. canibuy documents the same caveat
        # about its own opportunity ranking (service.py:404-406).
        "work_queue": sorted(
            work.values(),
            key=lambda w: (not w["unlockable"], -w["attempts_blocked"], -w["merchants_blocked"]),
        ),
        "drift": drift,
        # The registry is real even when the audit rows beside it are simulated.
        "provenance": {"simulated": 0, "total": graded, "label": ""},
    }
