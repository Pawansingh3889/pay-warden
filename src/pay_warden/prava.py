"""Prava backend: mints payment sessions for policy-approved requests.

Two modes, selected by PRAVA_MODE:
- "api": direct POST /v1/sessions against the sandbox with the merchant secret
  key (contract: prava-sdk-integration/references/session-api-reference.md).
- "cli": shells out to the linked `prava` CLI agent wallet
  (contract: prava-pay/references/cli-sessions.md).

The MCP server is the only caller; agents never touch this module's credentials.
"""

import os
import re
import subprocess

import httpx

from pay_warden.models import PurchaseRequest


class PravaError(RuntimeError):
    pass


class PravaSession(dict):
    """Result of a minted session: {'session_id': ..., 'payment_url': ...}."""


def create_session(req: PurchaseRequest) -> PravaSession:
    mode = os.environ.get("PRAVA_MODE", "api")
    if mode == "api":
        return _create_via_api(req)
    if mode == "cli":
        return _create_via_cli(req)
    raise PravaError(f"Unknown PRAVA_MODE: {mode!r} (expected 'api' or 'cli')")


def _create_via_api(req: PurchaseRequest) -> PravaSession:
    secret = os.environ.get("PRAVA_SECRET_KEY")
    if not secret:
        raise PravaError("PRAVA_SECRET_KEY is not set — copy .env.example to .env")
    base = os.environ.get("PRAVA_API_BASE", "https://sandbox.api.prava.space").rstrip("/")

    body = {
        "user_id": os.environ.get("PAY_WARDEN_USER_ID", "pay-warden-user"),
        "user_email": os.environ.get("PAY_WARDEN_USER_EMAIL", "user@example.com"),
        "total_amount": str(req.total_amount),
        "currency": req.currency,
        "description": f"pay-warden purchase by agent '{req.agent}'",
        "purchase_context": [
            {
                "merchant_details": {
                    "name": req.merchant_name,
                    "url": req.merchant_url,
                    "country_code_iso2": req.merchant_country,
                },
                "product_details": [
                    {
                        "description": p.description,
                        "unit_price": str(p.unit_price),
                        "quantity": p.quantity,
                    }
                    for p in req.products
                ],
            }
        ],
    }
    resp = httpx.post(
        f"{base}/v1/sessions",
        json=body,
        headers={"Authorization": f"Bearer {secret}"},
        timeout=30,
    )
    if resp.status_code != 201:
        raise PravaError(f"Prava sessions API returned {resp.status_code}: {resp.text}")
    data = resp.json()
    return PravaSession(session_id=data["session_id"], payment_url=data["iframe_url"])


def _create_via_cli(req: PurchaseRequest) -> PravaSession:
    cmd = [
        "prava", "sessions", "create",
        "--total-amount", str(req.total_amount),
        "--currency", req.currency,
        "--merchant-name", req.merchant_name,
        "--merchant-url", req.merchant_url,
        "--merchant-country", req.merchant_country,
    ]
    for p in req.products:
        cmd += [
            "--product",
            f'{{"description":"{p.description}","unit_price":"{p.unit_price}","quantity":{p.quantity}}}',
        ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise PravaError(f"prava CLI failed: {result.stderr.strip() or result.stdout.strip()}")

    session_id = _extract(r"Session ID:\s*(\S+)", result.stdout)
    payment_url = _extract(r"Payment URL:\s*(\S+)", result.stdout)
    return PravaSession(session_id=session_id, payment_url=payment_url)


def _extract(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise PravaError(f"Could not find {pattern!r} in prava CLI output:\n{text}")
    return match.group(1)
