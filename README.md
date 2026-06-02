# Mock Menta API

A stateful, in-memory mock of the [Menta payments API](https://developers.menta.global/),
built with FastAPI. Intended as a test target for an Elixir client.

Covers the two endpoint families the client uses:

- **Payment Intentions** (cloud terminals) — create / get / list / cancel
- **Transactions** — `GET /v2/transaction-reports`

Plus a lenient auth endpoint so a token flow can complete.

## Run

```bash
make run                      # installs deps + serves on http://127.0.0.1:8000
make dev                      # same, with auto-reload
make help                     # list all targets
```

Or without `make`:

```bash
pip install -r requirements.txt
python mock_menta.py          # http://127.0.0.1:8000
# or hot-reload:
uvicorn mock_menta:app --reload --port 8000
```

Interactive docs at <http://127.0.0.1:8000/docs>.
Interactive **terminal console** at <http://127.0.0.1:8000/console>.

## Marking intentions interactively

The mock can stand in for the physical terminal: your app creates an intention,
then *you* decide the outcome.

- **Web console** — open <http://127.0.0.1:8000/console> (or `make console`).
  Each pending intention has **Pay / Decline / Error** buttons; settled ones can
  be re-opened. Auto-refreshes every 2s.
- **CLI** — `make pay RID=<request_id>` (also `make decline`, `make error`).
- **HTTP** — `POST /__mock__/intentions/{request_id}/{pay|decline|error|pending}`.

By default intentions still auto-settle after a couple of seconds. To make them
wait for you instead, run in **manual mode** so they stay PENDING until marked:

```bash
make dev MANUAL=1            # or: MENTA_MOCK_MANUAL=1 python mock_menta.py
```

`error` produces a `FAILED` transaction, `decline` a `REJECTED` one, `pay` an
`APPROVED` one. A manual mark always wins over the auto-settle clock, and
`pending` re-opens an already-settled intention.

## How the stateful flow works

1. `POST /api/v1/cloud-terminals/payment-intentions` creates an intention.
   It returns **`201` with an empty body** (matching the real API). The client
   supplies its own `x-app-request-id` (UUID v4) header — that value becomes the
   `request_id`. Re-POSTing the same id is idempotent.
2. For the first `MENTA_MOCK_SETTLE_SECONDS` the intention is **PENDING**
   (delivery status `CREATED` → `DELIVERED`).
3. After it settles it becomes **APPROVED** (or `DECLINED`), delivery status
   `EXECUTED`, and a full `detail` object appears on the GET response.
4. A settled intention materializes a row in `GET /v2/transaction-reports`
   (`APPROVED` → transaction status `APPROVED`, `DECLINED` → `REJECTED`).

This lets you exercise client polling logic against PENDING→final transitions.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/apikey/merchant` | returns a fake Bearer token |
| GET  | `/auth/token/customer` | returns a fake Bearer token |
| POST | `/api/v1/cloud-terminals/payment-intentions` | create; `201` empty body |
| GET  | `/api/v1/cloud-terminals/payment-intentions` | list; `data[]` + `page{}` |
| GET  | `/api/v1/cloud-terminals/payment-intentions/{request_id}` | get one; `status` + `detail` |
| DELETE | `/api/v1/cloud-terminals/payment-intentions/{request_id}` | cancel (409 if already approved) |
| GET  | `/v2/transaction-reports` | list; `content[]` + `pageable{}` + totals |
| GET  | `/` , `/console` | interactive terminal console (HTML) |
| GET  | `/health` | mock liveness |
| GET  | `/__mock__/intentions` | console view of all intentions (both statuses) |
| POST | `/__mock__/intentions/{id}/{pay\|decline\|error\|pending}` | settle an intention |
| POST | `/__mock__/reset` | wipe in-memory state (test teardown) |

## Controlling outcomes

Global defaults (env vars):

| Var | Default | Meaning |
|---|---|---|
| `MENTA_MOCK_OUTCOME` | `APPROVED` | auto outcome for new intentions |
| `MENTA_MOCK_SETTLE_SECONDS` | `2` | seconds an intention stays PENDING |
| `MENTA_MOCK_MANUAL` | `0` | set `1` to hold intentions PENDING until marked by hand |
| `MENTA_MOCK_REQUIRE_AUTH` | `0` | set `1` to require a Bearer token (else 401) |

Per-request overrides (no restart):

- `x-mock-outcome: DECLINED` header — force one intention's outcome.
- `x-mock-settle-seconds: 0` header — settle immediately (no PENDING window).
- `x-mock-settle-seconds: never` header — hold PENDING until marked by hand.
- Put the word `DECLINE` in `additional_info` — force a decline.

## Example

```bash
B=http://127.0.0.1:8000
RID=$(uuidgen)

curl -X POST "$B/api/v1/cloud-terminals/payment-intentions" \
  -H "content-type: application/json" -H "x-app-request-id: $RID" \
  -d '{"customer_id":"cust-1","merchant_id":"merch-1","terminal_id":"term-1",
       "amount":"100.50","payment_method":"CREDIT","card_brand":"VISA","installments":3}'

curl "$B/api/v1/cloud-terminals/payment-intentions/$RID"   # PENDING, then APPROVED
curl "$B/v2/transaction-reports"                            # the settled transaction
```

## Notes / caveats

- Field shapes follow the public docs; nested `detail` / `operation_detail` /
  `tax_info` objects use representative sample values, not Menta's real schema in
  full. Adjust in `mock_menta.py` if your client asserts on specific fields.
- State is in-memory only — restarting (or `POST /__mock__/reset`) clears it.
- Auth is not validated by default; any/no token is accepted.
