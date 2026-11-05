# HOSTING.md — putting "Charge it once" live

The app is a single stdlib-only Python program. Any host that can run a
long-lived Python process and set environment variables can serve it
(a small PaaS web service, a container host, or a VM). There is no
build step, no dependency install, and no database.

## Start command

```sh
HOST=0.0.0.0 python3 app.py
```

- The app binds `HOST`:`PORT` (defaults `127.0.0.1:8000`). On a PaaS,
  set `HOST=0.0.0.0` and let the platform's `PORT` pass through.
- Runtime: Python 3.10 or newer.
- Health check / demo URL path: `GET /` (the demo page) and
  `GET /api/state` (JSON: config + permit snapshot + counters).

## Environment variables to set on the host

### A human must set these (secret values — they exist nowhere in this repo)

1. `PAYPAL_SANDBOX_CLIENT_ID` and `PAYPAL_SANDBOX_SECRET`
   - The PayPal Sandbox app credentials (developer.paypal.com →
     Apps & Credentials → Sandbox tab).
   - These were used transiently for the Gate 1 check and were
     **deliberately stored nowhere** in the workspace. A human has to
     read them from the PayPal developer dashboard and paste them into
     the host's env settings.
   - Without them the site still loads and the Agent Middleware leg
     works, but runs stop at "AMW authorized, PayPal not configured"
     and no PayPal order is created. The full demo needs them.

2. The Agent Middleware demo key, one of two ways:
   - `AMW_API_KEY` = the staging demo key value, pasted into the host's
     env settings, **or**
   - mount the existing key file on the host and set
     `AMW_API_KEY_FILE` to its path. The key already exists locally in
     a chmod-600 file at
     `/home/hatch/workspace/.jarvis/idea-executions/agentswap-retry-cases/.staging-key`
     (value intentionally not reproduced here). On most PaaS hosts the
     `AMW_API_KEY` env var is the practical option.

### Already known (safe to set as plain values, or leave defaulted)

- `AMW_WALLET_ID` = `agt-6d748f3a0fb7` (also the built-in default)
- `AMW_PERMIT_ID` = `permit-37b1597b97894ccd` (also the built-in default)
- `AMW_BASE_URL` — leave unset; defaults to the staging gateway
  `https://api-service-staging-staging-fb2e.up.railway.app`
- `PAYPAL_SANDBOX_BASE` — leave unset; defaults to
  `https://api-m.sandbox.paypal.com`

## Things to know before going live

- **Permit expiry:** the demo permit expires **2026-11-17**. That
  covers the Nov 12 submission deadline, but if the demo must stay live
  past Nov 17, a fresh permit must be issued for the same wallet first
  and `AMW_PERMIT_ID` updated. Permit budget is 25 credits; each
  distinct authorized action costs 1 credit (replays and denials cost
  0). A handful were already spent in local testing.
- **Run records are a local JSON file** (`DATA_DIR`, default `./data`).
  On hosts with an ephemeral filesystem the run history resets on
  redeploy/restart; the demo still works (counters rebuild from new
  runs, and receipts remain readable on the gateway). Attach a volume
  and point `DATA_DIR` at it if run history must persist.
- **Buyer approval step:** after a run creates a PayPal order, the demo
  page shows the Sandbox approve link. Whoever drives the demo clicks
  it, signs in with a PayPal Sandbox *buyer test account*, approves,
  returns to the page, and clicks capture. That is the real Sandbox
  checkout flow, not a simulation.
- **Outbound network:** the host must be able to reach
  `api-m.sandbox.paypal.com` and the staging gateway host above.
- **Do not** put any secret in the repo, in a Dockerfile, or in the
  host's start command — env settings only.
