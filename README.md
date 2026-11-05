# Charge it once

**Authorize one agent action. Charge it once. Prove what happened.**

A small agentic-commerce demo built for the PayPal AI Hackathon. An AI
agent proposes a purchase, but it cannot pay on its own. Every purchase
must pass through the Agent Middleware authorization loop first — under
an operator-issued, scoped permit — and only then does a PayPal Sandbox
order get created. The centerpiece is a live replay: submit the identical
action again with the same idempotency key and watch the second attempt
produce **no second debit and no second PayPal order**, with a signed
receipt to prove what happened.

The precise claim: **never a duplicate charge, not always a charge.**

All money movement in this demo is PayPal Sandbox only. Agent Middleware
credits are closed-loop pilot metering, not payment rails; the metering
debit and the PayPal order are shown side by side and never confused.

## How it works

1. **The agent proposes.** `agent.py` turns a purchase request into a
   structured proposal (item, amount, currency, payee) with a
   plain-language explanation. The agent cannot mint a permit, raise a
   budget, or bypass the authorization call.
2. **The operator's permit governs.** The app derives an idempotency key
   from the canonical action (SHA-256 of item + amount + currency +
   wallet + permit) and makes one governed call to Agent Middleware
   (`POST /mcp/messages`, tool in permit scope). One accepted key yields
   at most one gateway dispatch and at most one ledger debit.
3. **PayPal comes second.** Only after the authorization leg returns a
   successful receipt does the app create a PayPal Sandbox order
   (Orders API: create → buyer approval in Sandbox → capture). The
   PayPal order ID is bound into the run record next to the debit.
4. **Replay collapses.** Re-submitting the identical action re-invokes
   the gateway with the same key; the gateway returns the original
   receipt, and the app creates no second PayPal order for that key.
5. **Ambiguity stays honest.** If a capture outcome is treated as
   unconfirmed, the run is marked `delivery_uncertain` with its receipt
   and is never redispatched automatically. The reconcile step reads
   PayPal's own order record and settles the state from that.

## The five checks (what the demo proves)

1. **Happy path:** propose, approve, run. Exactly one ledger debit,
   exactly one PayPal Sandbox order, one signed receipt.
2. **Replay:** submit the identical action with the same key. Debit
   count stays 1; PayPal order count stays 1.
3. **Denial:** ask for a tool outside the permit's scope. The gateway
   denies it, charges nothing, and PayPal is never called. Denied calls
   are never charged.
4. **Uncertain outcome:** tick "simulate an unconfirmed capture
   outcome." The run is receipted as `delivery_uncertain`, nothing is
   redispatched, and Reconcile settles it from PayPal's order record.
5. **Receipt proof:** the receipt panel shows the receipt's binding —
   ledger entry, idempotency record, dispatch attempt — and whether it
   matches the gateway's live record. Receipts are Ed25519-signed;
   verify the signature offline with the standalone receipt verifier
   and the operator's public key.

Checks 1–4 are automated in `tests/local_checks.py` (see below).

## Run it

Requirements: Python 3.10+, standard library only. No install step.

```sh
cp .env.example .env   # then fill in values, or export them instead
python3 app.py
```

Open `http://127.0.0.1:8000`.

### Environment variables

| Variable | Required | Notes |
|---|---|---|
| `PAYPAL_SANDBOX_CLIENT_ID` | For the PayPal leg | Sandbox app client ID from developer.paypal.com |
| `PAYPAL_SANDBOX_SECRET` | For the PayPal leg | Sandbox app secret; never commit it |
| `PAYPAL_SANDBOX_BASE` | No | Defaults to `https://api-m.sandbox.paypal.com` |
| `AMW_API_KEY` | One of the two | The Agent Middleware demo key value |
| `AMW_API_KEY_FILE` | One of the two | Path to a chmod-600 file containing the key (local runs) |
| `AMW_BASE_URL` | No | Defaults to the staging gateway |
| `AMW_WALLET_ID` | No | Defaults to the demo allocation wallet `agt-6d748f3a0fb7` |
| `AMW_PERMIT_ID` | No | Defaults to `permit-37b1597b97894ccd` (partner.echo, 25 credits, expires 2026-11-17) |
| `HOST` / `PORT` | No | Defaults `127.0.0.1` / `8000`; hosts set `0.0.0.0` and their own `PORT` |
| `DATA_DIR` | No | Run records JSON; defaults to `./data` |

**Preview mode:** with no PayPal credentials set, the app still runs
and the Agent Middleware leg is fully real — runs end at
"AMW authorized, PayPal not configured" and the page says so plainly.
No PayPal order is ever fabricated in preview mode.

## Tests

```sh
AMW_API_KEY_FILE=/path/to/chmod600/staging-key python3 tests/local_checks.py
```

The checks run the real app against the **real** Agent Middleware
staging gateway (real permit, real receipts, real debits — budget about
2 credits per full run) and against a local stub of the PayPal Sandbox
Orders API (`tests/stub_paypal.py`) that implements the same calls the
Gate 1 script proved against the real sandbox (Gate 1 passed
2026-11-04: sandbox order created, buyer-approved, captured to
COMPLETED). The live sandbox leg cannot be part of the automated checks
because the sandbox credentials are deliberately stored nowhere; run
the app with real sandbox credentials to exercise it end to end.

## Layout

- `app.py` — the web app: demo page, run/replay/capture/reconcile flow, counters, receipt view
- `amw_client.py` — Agent Middleware gateway client (permits, governed invoke, receipts)
- `paypal_client.py` — PayPal Sandbox Orders client (same calls as the proven Gate 1 script)
- `agent.py` — the proposing agent (see the disclosure note inside)
- `tests/` — stub PayPal + automated checks 1–4
- `HOSTING.md` — exactly what a human must do to put this live
- `.env.example` — variable names only

## Tools / AI disclosure (for the submission)

- **PayPal Sandbox APIs** — Orders API (create, capture, get) via the
  client-credentials OAuth flow; the payment leg of the demo.
- **Agent Middleware** — the authorization layer: operator-issued
  scoped permits, exactly-once economic authorization, signed receipts.
- **Agent proposer** — `agent.py` exposes the proposal step behind one
  interface. This build ships a small deterministic proposer behind
  that interface so the demo runs with no extra model keys; describe
  the proposer you actually deploy, and do not describe the stand-in
  as an AI model.

## Licence

MIT — see `LICENSE`.
