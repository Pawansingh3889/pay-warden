"""Read-only operator dashboard.

Serves one self-contained page over the same audit trail the MCP server writes
to, so what a reader sees is the trail itself rather than a retelling of it.
Built on starlette + uvicorn, both already present via `mcp` — no new dependency.

This module is deliberately thin. Every number it serves is defined in
`insights.py`, which is where a figure on the page can be traced to the rows it
came from; nothing here derives a statistic of its own.

**The connection is read-only.** It used to construct an `AuditStore` per
request, which runs the schema DDL, three ALTER probes and a backfill UPDATE —
a write transaction every five seconds against the database the policy engine is
writing to, which also made this docstring's "read-only" untrue. Opening
`mode=ro` costs the ability to migrate, and that is the better trade: the page
can now be pointed at an older database and report what is missing instead of
silently rewriting somebody else's file.

Run: python -m pay_warden.dashboard   (then open http://127.0.0.1:8080)
"""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from pay_warden import canibuy_link, insights
from pay_warden.policy import Policy

_PAGE = Path(__file__).with_name("dashboard.html")
WINDOW_DAYS = 30
FEED_LIMIT = 200


def _db_path() -> str:
    return os.environ.get("PAY_WARDEN_DB", "pay_warden_audit.sqlite3")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    return conn


def _policy() -> Policy:
    return Policy.load(os.environ.get("PAY_WARDEN_POLICY", "policies/default.yaml"))


async def index(request) -> HTMLResponse:
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


async def state(request) -> JSONResponse:
    try:
        conn = _connect()
    except sqlite3.Error as exc:
        # No audit database yet is a normal state before the first purchase, not
        # a server fault. Say which file was looked for.
        return JSONResponse(
            {"error": f"could not open {_db_path()}: {exc}", "db": _db_path()}, status_code=503
        )
    try:
        payload = insights.build(
            conn,
            _policy(),
            now=datetime.now(UTC),
            window_days=WINDOW_DAYS,
            feed_limit=FEED_LIMIT,
            merchant_reader=canibuy_link.read,
        )
        payload["meta"]["db"] = _db_path()
        return JSONResponse(payload)
    finally:
        conn.close()


app = Starlette(routes=[Route("/", index), Route("/api/state", state)])


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("PAY_WARDEN_HOST", "127.0.0.1"),
        port=int(os.environ.get("PAY_WARDEN_PORT", "8080")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
