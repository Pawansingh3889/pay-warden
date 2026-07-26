"""Good-agent-vs-rogue-agent demo: drives the real MCP tools end to end.

Every scenario below calls the same functions an MCP client would call
(`preview_purchase`, `request_purchase`, `approve_purchase`, `get_audit_log`) —
nothing here reimplements the policy engine. Allowed requests mint a real Prava
sandbox session; denied ones never reach Prava at all, which is the point.

    python scripts/demo.py            # live — mints real sessions for allowed requests
    python scripts/demo.py --dry-run  # policy only, no network, no sessions minted

The demo uses its own audit DB and resets it on each run, so daily-budget and
velocity state start clean and the scenarios stay deterministic.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = ROOT / "pay_warden_demo.sqlite3"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"

VERDICT_STYLE = {
    "allowed": (GREEN, "ALLOWED"),
    "denied": (RED, "DENIED"),
    "needs_approval": (YELLOW, "NEEDS APPROVAL"),
}


def banner(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'━' * 74}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'━' * 74}{RESET}")


def show(scenario: str, narrative: str, result: dict) -> None:
    colour, label = VERDICT_STYLE.get(result["verdict"], (RESET, result["verdict"].upper()))
    print(f"\n{BOLD}{scenario}{RESET}")
    print(f"  {DIM}{narrative}{RESET}")
    print(f"  → {colour}{BOLD}{label}{RESET}  {DIM}rule:{RESET} {result['rule_id']}")
    print(f"    {result['reason']}")
    if result.get("payment_url"):
        print(f"    {GREEN}session{RESET}  {result['session_id']}")
        print(f"    {GREEN}pay at{RESET}  {result['payment_url']}")
    elif result["verdict"] == "denied":
        print(f"    {DIM}no Prava session minted — the request never left pay-warden{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate policy only; never call Prava (no sessions minted)",
    )
    args = parser.parse_args()

    # httpx logs every request to stderr, which interleaves badly with the
    # formatted output below; the demo narrates its own network calls.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    load_dotenv(ROOT / ".env")
    DEMO_DB.unlink(missing_ok=True)
    os.environ["PAY_WARDEN_DB"] = str(DEMO_DB)
    os.environ.setdefault("PAY_WARDEN_POLICY", str(ROOT / "policies" / "default.yaml"))

    if not args.dry_run and not os.environ.get("PRAVA_SECRET_KEY"):
        print(f"{RED}PRAVA_SECRET_KEY is not set — copy .env.example to .env, "
              f"or run with --dry-run{RESET}", file=sys.stderr)
        return 1

    from pay_warden import prava, server

    # Dry-run stubs only the network call, not the tool: the policy engine and
    # audit trail behave exactly as they do live, so budget and velocity state
    # accumulates the same way and a rehearsal matches the real run.
    if args.dry_run:
        counter = iter(range(1, 100))
        def _stub_session(req: object) -> prava.PravaSession:
            sid = f"ses_dryrun{next(counter):04d}"
            return prava.PravaSession(
                session_id=sid,
                payment_url=f"https://sandbox.collect.prava.space?session={sid}",
            )
        server.prava.create_session = _stub_session

    execute = server.request_purchase

    beans = [{"description": "Bag of beans, 250g", "unit_price": "18.50", "quantity": 1}]

    banner("1 · The good agent — a purchase that satisfies every rule")
    allowed = execute(
        agent="demo-shopper",
        merchant_name="Example Coffee Roasters",
        merchant_url="https://example.com",
        merchant_country="US",
        products=beans,
        total_amount="18.50",
        currency="USD",
    )
    show(
        "demo-shopper buys coffee — $18.50",
        "Within its $25 single-purchase cap and $50 daily budget, merchant not denied.",
        allowed,
    )

    banner("2 · The rogue agent — three ways the firewall says no")
    denied_merchant = execute(
        agent="rogue-agent",
        merchant_name="Sketchy Deals",
        merchant_url="https://sketchy-deals.example",
        merchant_country="US",
        products=[{"description": "Mystery box", "unit_price": "4.00", "quantity": 1}],
        total_amount="4.00",
        currency="USD",
    )
    show(
        "rogue-agent buys from a denied merchant — $4.00",
        "Well under every budget, but the merchant is on the deny-list.",
        denied_merchant,
    )

    denied_cap = execute(
        agent="rogue-agent",
        merchant_name="Example Coffee Roasters",
        merchant_url="https://example.com",
        merchant_country="US",
        products=[{"description": "Espresso machine", "unit_price": "25.00", "quantity": 1}],
        total_amount="25.00",
        currency="USD",
    )
    show(
        "rogue-agent overspends in one shot — $25.00",
        "Allowed merchant, but five times its $5 single-purchase cap.",
        denied_cap,
    )

    banner("3 · The budget runs out — same request, different answer")
    second = execute(
        agent="demo-shopper",
        merchant_name="Example Coffee Roasters",
        merchant_url="https://example.com",
        merchant_country="US",
        products=beans,
        total_amount="18.50",
        currency="USD",
    )
    show(
        "demo-shopper buys the same coffee again — $18.50",
        "Second purchase of the day: $37.00 of the $50 budget now committed.",
        second,
    )

    third = execute(
        agent="demo-shopper",
        merchant_name="Example Coffee Roasters",
        merchant_url="https://example.com",
        merchant_country="US",
        products=beans,
        total_amount="18.50",
        currency="USD",
    )
    show(
        "demo-shopper tries a third time — $18.50",
        "Identical to the two purchases that just succeeded — only the budget changed.",
        third,
    )

    banner("4 · The human gate — large requests park for approval")
    escalated = execute(
        agent="ops-agent",
        merchant_name="Example Coffee Roasters",
        merchant_url="https://example.com",
        merchant_country="US",
        products=[{"description": "Office espresso setup", "unit_price": "150.00", "quantity": 1}],
        total_amount="150.00",
        currency="USD",
    )
    show(
        "ops-agent makes a large purchase — $150.00",
        "Inside its own limits, but over the $100 auto-approval threshold.",
        escalated,
    )

    if escalated["verdict"] == "needs_approval":
        released = server.approve_purchase(escalated["attempt_id"])
        print(f"\n{BOLD}A human releases it{RESET}")
        print(f"  {DIM}approve_purchase({escalated['attempt_id']}){RESET}")
        if released.get("released"):
            print(f"  → {GREEN}{BOLD}RELEASED{RESET}")
            print(f"    {GREEN}session{RESET}  {released['session_id']}")
            print(f"    {GREEN}pay at{RESET}  {released['payment_url']}")
        else:
            print(f"  → {RED}{released.get('error')}{RESET}")

    banner("5 · The audit trail — every attempt, allowed or not")
    for row in server.get_audit_log():
        colour, label = VERDICT_STYLE.get(row["verdict"], (RESET, row["verdict"]))
        minted = row["session_id"] or f"{DIM}—{RESET}"
        print(
            f"  {row['ts'][11:19]}  {row['agent']:<13} "
            f"{row['total_amount']:>7} {row['currency']}  "
            f"{colour}{label:<14}{RESET} {row['rule_id']:<20} {minted}"
        )

    print(f"\n{DIM}Agents never see a card, a token, or a secret key — only decisions.{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
