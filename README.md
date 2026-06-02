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

## Deciding outcomes

**By default the mock accepts every intention and holds it `PENDING`** — it does
*not* approve anything on its own. You (or your test) decide the outcome, like an
operator at the physical terminal:

- **Web console** — open <http://127.0.0.1:8000/console> (or `make console`).
  Each pending intention has **Pay / Decline / Error** buttons; settled ones can
  be re-opened. Auto-refreshes every 2s.
- **CLI** — `make pay RID=<request_id>` (also `make decline`, `make error`).
- **HTTP** — `POST /__mock__/intentions/{request_id}/{pay|decline|error|pending}`.

`pay` → `APPROVED` (transaction `APPROVED`), `decline` → `DECLINED` (`REJECTED`),
`error` → `ERROR` (`FAILED`). `pending` re-opens an already-settled intention.

### Automatic response (top-right toggle)

To make the mock answer for you, set the **Automatic response** selector in the
top-right of the console to **Approve**, **Decline**, or **Error**. While set,
every *new* intention is settled to that outcome immediately; **Off** (the
default) goes back to holding them PENDING. It only affects intentions created
after you set it — existing PENDING ones are left alone.

Same thing over HTTP / at startup:

```bash
curl -X POST localhost:8000/__mock__/auto-response -H 'content-type: application/json' -d '{"value":"approve"}'
curl localhost:8000/__mock__/auto-response          # {"auto_response":"APPROVED"}
MENTA_MOCK_AUTO=approve python mock_menta.py         # start with it on
```

For scripted tests you can still override a single request without touching the
global setting (see the headers under *Controlling outcomes*).

## How the stateful flow works

1. `POST /api/v1/cloud-terminals/payment-intentions` creates an intention.
   It returns **`201` with `{"request_id": "..."}`** in the body (the public docs
   claim an empty body, but we believe the real API echoes the id). The client
   supplies its own `x-app-request-id` (UUID v4) header — that value becomes the
   `request_id`, also mirrored in the response header. Re-POSTing the same id is
   idempotent.
2. It stays **PENDING** (delivery status `CREATED` → `DELIVERED`) until you
   settle it — via the console, the CLI, or the *Automatic response* toggle.
3. Once settled it becomes **APPROVED** / **DECLINED** / **ERROR**, delivery
   status `EXECUTED`, and a full `detail` object appears on the GET response.
4. A settled intention materializes a row in `GET /v2/transaction-reports`
   (`APPROVED`→`APPROVED`, `DECLINED`→`REJECTED`, `ERROR`→`FAILED`).

This lets you exercise client polling logic against PENDING→final transitions.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/apikey/merchant` | returns a fake Bearer token |
| GET  | `/auth/token/customer` | returns a fake Bearer token |
| POST | `/api/v1/cloud-terminals/payment-intentions` | create; `201` + `{request_id}` |
| GET  | `/api/v1/cloud-terminals/payment-intentions` | list; `data[]` + `page{}` |
| GET  | `/api/v1/cloud-terminals/payment-intentions/{request_id}` | get one; `status` + `detail` |
| DELETE | `/api/v1/cloud-terminals/payment-intentions/{request_id}` | cancel (409 if already approved) |
| GET  | `/v2/transaction-reports` | list; `content[]` + `pageable{}` + totals |
| GET  | `/` , `/console` | interactive terminal console (HTML) |
| GET  | `/health` | mock liveness |
| GET  | `/__mock__/intentions` | console view of all intentions (both statuses) |
| POST | `/__mock__/intentions/{id}/{pay\|decline\|error\|pending}` | settle an intention |
| GET / POST | `/__mock__/auto-response` | read / set the automatic response |
| POST | `/__mock__/reset` | wipe in-memory state (test teardown) |

## Controlling outcomes

Global defaults (env vars):

| Var | Default | Meaning |
|---|---|---|
| `MENTA_MOCK_AUTO` | _(unset)_ | start with an automatic response on: `approve` / `decline` / `error` |
| `MENTA_MOCK_OUTCOME` | `APPROVED` | outcome used when an intention settles via a timer |
| `MENTA_MOCK_SETTLE_SECONDS` | _(unset → never)_ | set a number to auto-settle after N seconds (old timer behaviour) |
| `MENTA_MOCK_REQUIRE_AUTH` | `0` | set `1` to require a Bearer token (else 401) |

Per-request overrides (no restart, and they bypass the automatic response):

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
