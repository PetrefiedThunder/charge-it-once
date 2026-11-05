#!/usr/bin/env python3
"""Local end-to-end checks for the "Charge it once" prototype.

Runs the real app against the REAL Agent Middleware staging gateway
(the authorization leg is not stubbed: real permit, real receipts, real
debits) and against a local stub of the PayPal Sandbox Orders API (the
PayPal leg uses the same calls the Gate 1 script proved against the
real sandbox; the live sandbox cannot be used here because the sandbox
credentials are not stored anywhere).

Each governed invoke that succeeds costs 1 staging credit from the demo
permit (25 max). Expected spend for a full run: 2 credits (happy path +
uncertain run). Replays and denials cost 0 additional credits.

Usage:
  AMW_API_KEY_FILE=/path/to/chmod600/key python3 tests/local_checks.py

Exit code 0 = all checks passed.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
STUB_PORT = 8399
APP_PORT = 8417
STUB = f"http://127.0.0.1:{STUB_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"
RUN_TAG = uuid.uuid4().hex[:8]

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as resp:
        text = resp.read().decode()
        return resp.status, json.loads(text) if text else {}


def wait_up(base, path="/", tries=40):
    for _ in range(tries):
        try:
            call(base, "GET", path)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    env = dict(os.environ)
    env.update({
        "PAYPAL_SANDBOX_CLIENT_ID": "stub-client",
        "PAYPAL_SANDBOX_SECRET": "stub-secret",
        "PAYPAL_SANDBOX_BASE": STUB,
        "HOST": "127.0.0.1",
        "PORT": str(APP_PORT),
        "DATA_DIR": f"/tmp/cio-test-data-{RUN_TAG}",
    })
    if not env.get("AMW_API_KEY_FILE") and not env.get("AMW_API_KEY"):
        print("Set AMW_API_KEY_FILE (chmod-600 staging key file) first.")
        return 2
    stub = subprocess.Popen([sys.executable,
                             os.path.join(HERE, "stub_paypal.py"),
                             str(STUB_PORT)])
    app = subprocess.Popen([sys.executable, os.path.join(APP_DIR, "app.py")],
                           env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_up(STUB, "/__count"), "stub did not start"
        assert wait_up(APP, "/api/state"), "app did not start"

        # Page loads and carries the concept.
        with urllib.request.urlopen(APP + "/", timeout=10) as resp:
            page = resp.read().decode()
        check("demo page loads", "Charge it once" in page)

        item = f"Demo notebook {RUN_TAG}"
        # 1. Happy path: propose -> run -> approve (stub) -> capture.
        _, prop = call(APP, "POST", "/api/propose",
                       {"item": item, "amount": 1.00})
        check("proposal + idempotency key",
              prop["proposal"]["amount"] == 1.00 and
              prop["idempotency_key"].startswith("charge-it-once-"))
        _, out = call(APP, "POST", "/api/run", {"item": item, "amount": 1.00})
        run = out["run"]
        rec = run.get("receipt") or {}
        check("AMW leg: real receipt, 1 credit",
              run["status"] == "awaiting_buyer_approval" and
              rec.get("receipt_id", "").startswith("rcpt-") and
              float(rec.get("credits_charged") or 0) == 1.0,
              f"receipt={rec.get('receipt_id')}")
        order_id = run["paypal_order_id"]
        call(STUB, "GET", f"/approve?token={order_id}")  # buyer approval
        _, out = call(APP, "POST", "/api/capture", {"run_id": run["run_id"]})
        check("PayPal leg: capture COMPLETED",
              out["run"]["status"] == "confirmed" and
              bool(out["run"]["paypal_capture_id"]))
        _, counts = call(STUB, "GET", "/__count")
        check("exactly one PayPal order so far", counts["orders"] == 1)

        # 2. Replay: same key -> original receipt, no second order/debit.
        _, out = call(APP, "POST", "/api/replay", {"run_id": run["run_id"]})
        check("replay returns the original receipt",
              out["replay"]["receipt_same_as_original"] is True and
              out["replay"]["paypal_order_created"] is False)
        check("counters after replay: 1 debit, 1 order",
              out["counters"]["ledger_debits"] == 1 and
              out["counters"]["paypal_orders"] == 1,
              json.dumps(out["counters"]))
        _, counts = call(STUB, "GET", "/__count")
        check("stub still holds exactly one order", counts["orders"] == 1)

        # Receipt binding view.
        _, view = call(APP, "GET", f"/api/receipt?run_id={run['run_id']}")
        check("receipt binding: ids + signature present, order bound",
              view.get("signature_present") is True and
              view.get("matches_live_gateway_record") is True and
              view.get("paypal_order_id_bound_in_run") == order_id)

        # 3. Denial: out-of-scope tool -> denied, no charge, no order.
        _, out = call(APP, "POST", "/api/run",
                      {"item": f"Out of scope {RUN_TAG}", "amount": 1.00,
                       "tool": "partner.other"})
        drec = out["run"].get("receipt") or {}
        check("out-of-scope tool denied, never charged, no PayPal order",
              out["run"]["status"] == "denied" and
              not float(drec.get("credits_charged") or 0) and
              out["run"]["paypal_order_id"] is None)
        _, counts = call(STUB, "GET", "/__count")
        check("stub order count unchanged after denial", counts["orders"] == 1)

        # 4. Uncertain outcome: no redispatch; reconcile settles from the
        # external record.
        _, out = call(APP, "POST", "/api/run",
                      {"item": f"Uncertain widget {RUN_TAG}", "amount": 1.00,
                       "simulate_uncertain": True})
        run2 = out["run"]
        call(STUB, "GET", f"/approve?token={run2['paypal_order_id']}")
        _, out = call(APP, "POST", "/api/capture",
                      {"run_id": run2["run_id"]})
        check("unconfirmed capture -> delivery_uncertain",
              out["run"]["status"] == "delivery_uncertain")
        _, out = call(APP, "POST", "/api/reconcile",
                      {"run_id": run2["run_id"]})
        check("reconcile settles from PayPal's record, no new order",
              out["run"]["status"] == "confirmed" and
              bool(out["run"]["paypal_capture_id"]))
        _, counts = call(STUB, "GET", "/__count")
        check("two orders total (one per distinct action, never a replay)",
              counts["orders"] == 2)
        _, state = call(APP, "GET", "/api/state")
        check("final counters: 2 debits, 2 orders",
              state["counters"]["ledger_debits"] == 2 and
              state["counters"]["paypal_orders"] == 2,
              json.dumps(state["counters"]))
    finally:
        app.terminate()
        stub.terminate()
        try:
            out, _ = app.communicate(timeout=5)
            tail = "\n".join(out.splitlines()[-5:])
            print("--- app output tail ---\n" + tail)
        except Exception:
            pass
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
