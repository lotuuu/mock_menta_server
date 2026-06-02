"""
Mock server for the Menta payments API (https://developers.menta.global/).

Scope: Payment Intentions (cloud terminals) + Transactions reports.
Behaviour: stateful, in-memory. A created payment intention progresses over
time from PENDING to a final outcome (APPROVED by default); once it settles it
materializes a transaction that shows up in the /v2/transaction-reports endpoint.

This is meant as a fixture target for an Elixir client under test, NOT a faithful
re-implementation of Menta's business logic.

Run:
    pip install -r requirements.txt
    python mock_menta.py            # serves on http://127.0.0.1:8000
    # or: uvicorn mock_menta:app --reload --port 8000

By default every intention is accepted and held PENDING — nothing is approved
automatically. Settle them from the console (http://127.0.0.1:8000/console), the
CLI, or by turning on the console's "Automatic response" toggle.

Useful knobs (env vars):
    MENTA_MOCK_AUTO             start with an automatic response on:
                                approve | decline | error  (default: off)
    MENTA_MOCK_OUTCOME          outcome used if an intention settles via a timer
    MENTA_MOCK_SETTLE_SECONDS   set a number to auto-settle after N seconds
                                (unset => hold PENDING until settled by hand)
    MENTA_MOCK_REQUIRE_AUTH     "1" to reject requests without a Bearer token

Per-request overrides (no restart, and they bypass the automatic response):
    - Send header  x-mock-outcome: DECLINED   to force one intention's outcome.
    - Put the word DECLINE anywhere in `additional_info` to force a decline.
    - Send header  x-mock-settle-seconds: 0    to settle immediately.
    - Send header  x-mock-settle-seconds: never to keep it PENDING until marked.

Interactive control:
    - Open  http://127.0.0.1:8000/console  to mark intentions paid/errored and
      set the automatic response (top-right).
    - Or POST /__mock__/intentions/{request_id}/{pay|decline|error|pending}.
    - Or POST /__mock__/auto-response  {"value": "approve|decline|error|off"}.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_OUTCOME = os.getenv("MENTA_MOCK_OUTCOME", "APPROVED").upper()
REQUIRE_AUTH = os.getenv("MENTA_MOCK_REQUIRE_AUTH", "0") == "1"

# By default an intention is just accepted and held PENDING until you settle it
# (via the console buttons or the control API). Set MENTA_MOCK_SETTLE_SECONDS to
# a number to restore the old "auto-settle after N seconds" timer behaviour.
_settle_env = os.getenv("MENTA_MOCK_SETTLE_SECONDS")
DEFAULT_SETTLE_SECONDS = float(_settle_env) if _settle_env not in (None, "") else math.inf

# Outcomes that can be applied to an intention (manual overrides + auto).
NEGATIVE_OUTCOMES = {"DECLINED", "ERROR"}
FINAL_OUTCOMES = {"APPROVED"} | NEGATIVE_OUTCOMES

# Map friendly action words -> the outcome applied to an intention. None means
# "no automatic outcome — leave it PENDING".
OUTCOME_ALIASES = {
    "pay": "APPROVED", "paid": "APPROVED", "approve": "APPROVED", "approved": "APPROVED",
    "decline": "DECLINED", "declined": "DECLINED", "reject": "DECLINED",
    "error": "ERROR", "fail": "ERROR", "failed": "ERROR",
    "off": None, "none": None, "manual": None, "": None,
}


def normalize_outcome(value: Optional[str]) -> Optional[str]:
    """Resolve a string to APPROVED/DECLINED/ERROR or None; raise on garbage."""
    if value is None:
        return None
    key = value.strip().lower()
    if key in OUTCOME_ALIASES:
        return OUTCOME_ALIASES[key]
    if value.strip().upper() in FINAL_OUTCOMES:
        return value.strip().upper()
    raise ValueError(f"unknown outcome '{value}'")


# Runtime-mutable global settings (changed from the console at /console).
STATE: dict[str, Any] = {
    # When set to APPROVED/DECLINED/ERROR, new intentions get that outcome
    # automatically. When None, new intentions stay PENDING until settled by hand.
    "auto_response": normalize_outcome(os.getenv("MENTA_MOCK_AUTO")),
}

API_PREFIX = "/api/v1"

app = FastAPI(title="Mock Menta API", version="mock-1.0")


# --------------------------------------------------------------------------- #
# In-memory store
# --------------------------------------------------------------------------- #


class Intention:
    """A cloud-terminal payment intention and its derived state."""

    def __init__(self, request_id: str, body: "CreateIntentionBody", outcome: str,
                 settle_seconds: float):
        self.request_id = request_id
        self.customer_id = body.customer_id
        self.merchant_id = body.merchant_id
        self.terminal_id = body.terminal_id
        self.amount_str = body.amount               # original decimal string
        self.amount_cents = _to_cents(body.amount)
        self.payment_method = body.payment_method or "CREDIT"
        self.card_brand = body.card_brand or "VISA"
        self.installments = body.installments or 1
        self.additional_info = body.additional_info
        self.is_tip_allowed = body.is_tip_allowed
        self.is_print_allowed = body.is_print_allowed

        self.flow = "PAYMENT_INTENT"
        self.created_at = time.time()
        self.cancelled = False
        self.outcome = outcome                      # auto outcome once settle elapses
        self.settle_seconds = settle_seconds
        # When set (via the console / control API) this wins over time-based
        # settling: APPROVED, DECLINED, ERROR, or PENDING to hold it open.
        self.manual_outcome: Optional[str] = None
        self.transaction_id = str(uuid.uuid4())
        self.operation_id = str(uuid.uuid4())

    # -- derived, time-based state ----------------------------------------- #

    @property
    def settled(self) -> bool:
        return (time.time() - self.created_at) >= self.settle_seconds

    @property
    def payment_status(self) -> str:
        """APPROVED / DECLINED / ERROR / PENDING — the operation outcome."""
        if self.cancelled:
            return "CANCELLED"
        if self.manual_outcome == "PENDING":
            return "PENDING"           # explicitly held open
        if self.manual_outcome:
            return self.manual_outcome  # manual mark wins over the clock
        if not self.settled:
            return "PENDING"
        return self.outcome

    @property
    def delivery_status(self) -> str:
        """CREATED / DELIVERED / EXECUTED — the cloud command delivery state."""
        if self.cancelled:
            return "NOT_DELIVERED"
        if self.payment_status in FINAL_OUTCOMES:
            return "EXECUTED"
        if self.payment_status == "PENDING" and self.manual_outcome is None:
            elapsed = time.time() - self.created_at
            half = self.settle_seconds / 2 if self.settle_seconds != math.inf else math.inf
            return "CREATED" if elapsed < half else "DELIVERED"
        return "DELIVERED"

    # -- serialization ----------------------------------------------------- #

    def as_detail(self) -> Optional[dict[str, Any]]:
        if self.payment_status not in FINAL_OUTCOMES:
            return None
        return {
            "transaction_id": self.transaction_id,
            "operation_id": self.operation_id,
            "operation_type": "PAYMENT",
            "payment_method": self.payment_method,
            "status": self.payment_status,
            "currency": "ARS",
            "installments": self.installments,
            "financing": "ESTANDAR",
            "acquirer": "PRISMA",
            "card_bin": "411111",
            "card_mask": "411111******1111",
            "card_brand": self.card_brand,
            "is_international_card": False,
            "input_mode": "CONTACTLESS",
            "authorization_code": "123456" if self.payment_status == "APPROVED" else None,
            "tax_info": {
                "net_amount": round(self.amount_cents / 100, 2),
                "term": 1,
                "payment_date": _iso(self.created_at),
            },
        }

    def as_get_response(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "customer_id": self.customer_id,
            "merchant_id": self.merchant_id,
            "terminal_id": self.terminal_id,
            "amount": self.amount_cents,            # cents, per docs
            "status": self.payment_status,
            "operation_additional_info": self.additional_info,
            "detail": self.as_detail(),
        }

    def as_list_item(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "merchant_id": self.merchant_id,
            "terminal_id": self.terminal_id,
            "request_id": self.request_id,
            "type": "CLOUD_TERMINAL",
            "status": self.delivery_status,
            "created_at": _iso(self.created_at),
            "data": {
                "flow": "PAYMENT_CANCELLATION" if self.cancelled else self.flow,
                "amount": self.amount_str,
                "payment_method": self.payment_method,
                "card_brand": self.card_brand,
                "installments": self.installments,
                "additional_info": self.additional_info,
                "is_print_allowed": self.is_print_allowed,
                "is_tip_allowed": self.is_tip_allowed,
            },
        }

    def as_transaction(self) -> dict[str, Any]:
        approved = self.payment_status == "APPROVED"
        # APPROVED -> APPROVED, ERROR -> FAILED, DECLINED -> REJECTED
        txn_status = {
            "APPROVED": "APPROVED",
            "ERROR": "FAILED",
            "DECLINED": "REJECTED",
        }.get(self.payment_status, "REJECTED")
        return {
            "transaction_id": self.transaction_id,
            "operation_id": self.operation_id,
            "operation_number": self.operation_id[:8],
            "operation_type": "PAYMENT",
            "payment_method": self.payment_method,
            "status": txn_status,
            "gross_amount": round(self.amount_cents / 100, 2),
            "currency": "ARS",
            "datetime": _iso(self.created_at),
            "installments": self.installments,
            "acquirer": "PRISMA",
            "user": self.merchant_id,
            "merchant_id": self.merchant_id,
            "operation_detail": {
                "card_bin": "411111",
                "card_mask": "411111******1111",
                "card_brand": self.card_brand,
                "card_type": self.payment_method,
                "input_mode": "CONTACTLESS",
                "authorization_code": "123456" if approved else None,
                "rrn": self.operation_id[:12],
            },
            "tax_info": {
                "payment_terms": self.installments,
                "net_amount": round(self.amount_cents / 100, 2),
                "taxes": [],
            },
        }


# requestId -> Intention
INTENTIONS: dict[str, Intention] = {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _to_cents(amount: str) -> int:
    try:
        return int(round(float(amount) * 100))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount must be a numeric string")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_auth(authorization: Optional[str]) -> None:
    if REQUIRE_AUTH and not (authorization or "").lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")


def _parse_settle(raw: Optional[str], default: float) -> float:
    """Parse the x-mock-settle-seconds header. 'never'/negative => never settle."""
    if raw is None:
        return default
    if raw.strip().lower() in ("never", "inf", "infinity"):
        return math.inf
    try:
        val = float(raw)
    except ValueError:
        return default
    return math.inf if val < 0 else val


def _settled_intentions() -> list[Intention]:
    return [i for i in INTENTIONS.values() if i.payment_status in FINAL_OUTCOMES]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class ApiKeyAuthBody(BaseModel):
    api_key: Optional[str] = None
    apikey: Optional[str] = None


class CreateIntentionBody(BaseModel):
    customer_id: str
    merchant_id: str
    terminal_id: str
    amount: str
    payment_method: Optional[str] = None
    card_brand: Optional[str] = None
    installments: Optional[int] = Field(default=1)
    additional_info: Optional[str] = None
    is_tip_allowed: bool = True
    is_print_allowed: bool = True


# --------------------------------------------------------------------------- #
# Auth (lenient — included so the Elixir client can complete a token flow)
# --------------------------------------------------------------------------- #


@app.post("/auth/apikey/merchant")
def auth_apikey_merchant(body: ApiKeyAuthBody):
    return {
        "access_token": "mock-merchant-token-" + uuid.uuid4().hex,
        "token_type": "Bearer",
        "expires_in": 3600,
        "user_type": "MERCHANT",
    }


@app.get("/auth/token/customer")
def auth_token_customer():
    return {
        "access_token": "mock-customer-token-" + uuid.uuid4().hex,
        "token_type": "Bearer",
        "expires_in": 3600,
        "user_type": "CUSTOMER",
    }


# --------------------------------------------------------------------------- #
# Payment Intentions
# --------------------------------------------------------------------------- #


@app.post(f"{API_PREFIX}/cloud-terminals/payment-intentions", status_code=201)
def create_payment_intention(
    body: CreateIntentionBody,
    authorization: Optional[str] = Header(default=None),
    x_app_request_id: Optional[str] = Header(default=None),
    x_mock_outcome: Optional[str] = Header(default=None),
    x_mock_settle_seconds: Optional[str] = Header(default=None),
):
    _check_auth(authorization)

    request_id = x_app_request_id or str(uuid.uuid4())

    # Idempotency: re-POSTing the same x-app-request-id returns the existing one.
    if request_id in INTENTIONS:
        return JSONResponse(
            status_code=201,
            content={"request_id": request_id},
            headers={"x-app-request-id": request_id},
        )

    outcome = (x_mock_outcome or DEFAULT_OUTCOME).upper()
    if body.additional_info and "DECLINE" in body.additional_info.upper():
        outcome = "DECLINED"
    if outcome not in FINAL_OUTCOMES:
        outcome = "APPROVED"

    settle = _parse_settle(x_mock_settle_seconds, DEFAULT_SETTLE_SECONDS)

    # Per-request headers win. Otherwise, if the console's "automatic response"
    # is set, settle the new intention to it immediately; if not, hold it PENDING.
    if x_mock_outcome is None and x_mock_settle_seconds is None and STATE["auto_response"]:
        outcome = STATE["auto_response"]
        settle = 0

    INTENTIONS[request_id] = Intention(request_id, body, outcome, settle)

    # Docs claim a 201 with an empty body, but we believe the real API echoes the
    # assigned request_id, so we return it (also mirrored in the header).
    return JSONResponse(
        status_code=201,
        content={"request_id": request_id},
        headers={"x-app-request-id": request_id},
    )


@app.get(f"{API_PREFIX}/cloud-terminals/payment-intentions")
def list_payment_intentions(
    authorization: Optional[str] = Header(default=None),
    terminalId: Optional[str] = Query(default=None),
    requestId: Optional[str] = Query(default=None),
    flow: Optional[str] = Query(default=None),
    page: int = Query(default=0, ge=0),
    size: int = Query(default=10, ge=1),
):
    _check_auth(authorization)

    items = list(INTENTIONS.values())
    if terminalId:
        items = [i for i in items if i.terminal_id == terminalId]
    if requestId:
        items = [i for i in items if i.request_id == requestId]
    items.sort(key=lambda i: i.created_at, reverse=True)

    total = len(items)
    start = page * size
    window = items[start:start + size]

    return {
        "data": [i.as_list_item() for i in window],
        "page": {
            "size": size,
            "total_elements": total,
            "total_pages": (total + size - 1) // size if size else 0,
            "number": page,
        },
    }


@app.get(f"{API_PREFIX}/cloud-terminals/payment-intentions/{{request_id}}")
def get_payment_intention(
    request_id: str,
    authorization: Optional[str] = Header(default=None),
    merchantId: Optional[str] = Query(default=None),
):
    _check_auth(authorization)
    intention = INTENTIONS.get(request_id)
    if not intention:
        raise HTTPException(status_code=404, detail="Payment intention not found")
    return intention.as_get_response()


@app.delete(f"{API_PREFIX}/cloud-terminals/payment-intentions/{{request_id}}", status_code=200)
def cancel_payment_intention(
    request_id: str,
    authorization: Optional[str] = Header(default=None),
    x_app_request_id: Optional[str] = Header(default=None),
):
    _check_auth(authorization)
    intention = INTENTIONS.get(request_id)
    if not intention:
        raise HTTPException(status_code=404, detail="Payment intention not found")
    if intention.payment_status == "APPROVED":
        raise HTTPException(status_code=409, detail="Cannot cancel a settled (approved) intention")
    intention.cancelled = True
    return {"request_id": request_id, "status": "CANCELLED"}


# --------------------------------------------------------------------------- #
# Transactions (v2 reports) — reflects settled intentions
# --------------------------------------------------------------------------- #


@app.get("/v2/transaction-reports")
def list_transactions(
    authorization: Optional[str] = Header(default=None),
    page: int = Query(default=0, ge=0),
    size: int = Query(default=50, ge=1, le=10000),
    merchantId: Optional[str] = Query(default=None),
    operationId: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    paymentMethod: Optional[str] = Query(default=None),
):
    _check_auth(authorization)

    txns = _settled_intentions()
    if merchantId:
        txns = [i for i in txns if i.merchant_id == merchantId]
    if operationId:
        txns = [i for i in txns if i.operation_id == operationId]
    if paymentMethod:
        txns = [i for i in txns if i.payment_method == paymentMethod.upper()]

    rows = [i.as_transaction() for i in txns]
    if status:
        rows = [r for r in rows if r["status"] == status.upper()]
    rows.sort(key=lambda r: r["datetime"], reverse=True)

    total = len(rows)
    start = page * size
    window = rows[start:start + size]

    return {
        "content": window,
        "pageable": {
            "page_number": page,
            "page_size": size,
            "offset": start,
        },
        "total_pages": (total + size - 1) // size if size else 0,
        "total_elements": total,
    }


# --------------------------------------------------------------------------- #
# Test / control helpers (not part of the real API)
# --------------------------------------------------------------------------- #


@app.get("/health")
def health():
    return {"status": "ok", "intentions": len(INTENTIONS)}


@app.post("/__mock__/reset")
def reset_state():
    """Wipe all in-memory state — handy in test setup/teardown."""
    INTENTIONS.clear()
    return {"status": "reset"}


class AutoResponseBody(BaseModel):
    # Accepts APPROVED/DECLINED/ERROR or friendly aliases (pay/decline/error),
    # and off/none/manual/null to disable automatic responses.
    value: Optional[str] = None


@app.get("/__mock__/auto-response")
def get_auto_response():
    """Current automatic-response setting (null = accept & hold PENDING)."""
    return {"auto_response": STATE["auto_response"]}


@app.post("/__mock__/auto-response")
def set_auto_response(body: AutoResponseBody):
    """Set the automatic response applied to NEW intentions."""
    try:
        STATE["auto_response"] = normalize_outcome(body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"auto_response": STATE["auto_response"]}


@app.get("/__mock__/intentions")
def console_intentions():
    """Console-friendly view of every intention with both status flavours."""
    items = sorted(INTENTIONS.values(), key=lambda i: i.created_at, reverse=True)
    return [
        {
            "request_id": i.request_id,
            "merchant_id": i.merchant_id,
            "terminal_id": i.terminal_id,
            "amount": i.amount_str,
            "payment_method": i.payment_method,
            "installments": i.installments,
            "additional_info": i.additional_info,
            "payment_status": i.payment_status,
            "delivery_status": i.delivery_status,
            "created_at": _iso(i.created_at),
        }
        for i in items
    ]


# pay/decline/error/pending -> the manual outcome applied to an intention.
_MARK_ALIASES = {
    "pay": "APPROVED",
    "paid": "APPROVED",
    "approve": "APPROVED",
    "approved": "APPROVED",
    "decline": "DECLINED",
    "declined": "DECLINED",
    "reject": "DECLINED",
    "error": "ERROR",
    "fail": "ERROR",
    "failed": "ERROR",
    "pending": "PENDING",
    "reset": None,  # clear the manual override, fall back to time-based
}


@app.post("/__mock__/intentions/{request_id}/{action}")
def mark_intention(request_id: str, action: str):
    """Interactively settle an intention: pay / decline / error / pending."""
    intention = INTENTIONS.get(request_id)
    if not intention:
        raise HTTPException(status_code=404, detail="Payment intention not found")

    key = action.lower()
    if key not in _MARK_ALIASES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{action}'. Use one of: {sorted(_MARK_ALIASES)}",
        )

    intention.manual_outcome = _MARK_ALIASES[key]
    intention.cancelled = False
    return {
        "request_id": request_id,
        "payment_status": intention.payment_status,
        "delivery_status": intention.delivery_status,
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/console", response_class=HTMLResponse)
def console_page():
    return CONSOLE_HTML


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock Menta — terminal console</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         margin: 0; padding: 24px; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 16px; margin: 0 0 4px; }
  .sub { color: #8a8f98; margin: 0 0 20px; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #23262d; }
  th { color: #8a8f98; font-weight: 600; font-size: 11px; text-transform: uppercase;
       letter-spacing: .04em; }
  td.rid { font-size: 11px; color: #9aa0aa; }
  .badge { padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; }
  .PENDING { background: #3a3000; color: #ffd84d; }
  .APPROVED { background: #04341f; color: #4ade80; }
  .DECLINED { background: #3a0d12; color: #fb7185; }
  .ERROR { background: #3a0d12; color: #fb7185; }
  .CANCELLED { background: #2a2d34; color: #9aa0aa; }
  button { font: inherit; cursor: pointer; border: 1px solid #2c2f38; background: #1a1d24;
           color: #e6e6e6; border-radius: 6px; padding: 4px 10px; margin-right: 4px; }
  button:hover { border-color: #4b5563; }
  button.pay:hover { border-color: #4ade80; color: #4ade80; }
  button.decline:hover, button.err:hover { border-color: #fb7185; color: #fb7185; }
  .empty { color: #8a8f98; padding: 40px 0; text-align: center; }
  .meta { color: #8a8f98; font-size: 12px; }
  .topbar { display: flex; justify-content: space-between; align-items: flex-start;
            gap: 16px; margin-bottom: 20px; }
  .auto { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #8a8f98;
          white-space: nowrap; }
  .auto.on { color: #e6e6e6; }
  .auto .dot { width: 8px; height: 8px; border-radius: 50%; background: #3a3f4a; }
  .auto.on .dot { background: #4ade80; box-shadow: 0 0 6px #4ade80; }
  .auto select { font: inherit; background: #1a1d24; color: #e6e6e6;
                 border: 1px solid #2c2f38; border-radius: 6px; padding: 4px 8px; }
</style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>Mock Menta — terminal console</h1>
      <p class="sub">New intentions are accepted and held <b>PENDING</b> until you decide,
         like an operator at the physical terminal. Auto-refreshes every 2s.</p>
    </div>
    <label class="auto" id="autoBox" title="Automatically settle incoming intentions">
      <span class="dot"></span> Automatic response:
      <select id="autoSel" onchange="setAuto(this.value)">
        <option value="off">Off — hold pending</option>
        <option value="approve">Approve</option>
        <option value="decline">Decline</option>
        <option value="error">Error</option>
      </select>
    </label>
  </div>
  <table>
    <thead>
      <tr><th>Request id</th><th>Amount</th><th>Method</th><th>Payment</th>
          <th>Delivery</th><th>Created</th><th>Action</th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <p id="empty" class="empty" hidden>No payment intentions yet. Create one from your app.</p>

<script>
const AUTO_TO_OPT = { APPROVED: 'approve', DECLINED: 'decline', ERROR: 'error' };
async function mark(rid, action) {
  await fetch(`/__mock__/intentions/${rid}/${action}`, { method: 'POST' });
  load();
}
async function setAuto(value) {
  await fetch('/__mock__/auto-response', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ value })
  });
  loadAuto();
}
async function loadAuto() {
  const { auto_response } = await (await fetch('/__mock__/auto-response')).json();
  document.getElementById('autoSel').value = AUTO_TO_OPT[auto_response] || 'off';
  document.getElementById('autoBox').classList.toggle('on', !!auto_response);
}
function badge(s) { return `<span class="badge ${s}">${s}</span>`; }
function actions(row) {
  if (row.payment_status === 'PENDING') {
    return `<button class="pay" onclick="mark('${row.request_id}','pay')">Pay</button>`
         + `<button class="decline" onclick="mark('${row.request_id}','decline')">Decline</button>`
         + `<button class="err" onclick="mark('${row.request_id}','error')">Error</button>`;
  }
  return `<button onclick="mark('${row.request_id}','pending')">Re-open</button>`;
}
async function load() {
  const data = await (await fetch('/__mock__/intentions')).json();
  const rows = document.getElementById('rows');
  document.getElementById('empty').hidden = data.length > 0;
  rows.innerHTML = data.map(r => `<tr>
    <td class="rid">${r.request_id}</td>
    <td>${r.amount}</td>
    <td class="meta">${r.payment_method}${r.installments > 1 ? ' x'+r.installments : ''}</td>
    <td>${badge(r.payment_status)}</td>
    <td class="meta">${r.delivery_status}</td>
    <td class="meta">${r.created_at}</td>
    <td>${actions(r)}</td>
  </tr>`).join('');
}
loadAuto();
load();
setInterval(load, 2000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
