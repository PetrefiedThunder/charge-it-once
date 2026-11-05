#!/usr/bin/env python3
"""Charge it once — PayPal AI Hackathon prototype.

Authorize one agent action. Charge it once. Prove what happened.

Flow:
  1. The agent proposes a purchase (agent.py). It cannot authorize.
  2. The operator's scoped permit governs the Agent Middleware leg:
     one governed invoke with an idempotency key derived from the
     canonical action. One accepted key = at most one dispatch and at
     most one ledger debit. Denied calls are never charged.
  3. Only after the AMW leg succeeds does the app create a PayPal
     Sandbox order. The buyer approves in Sandbox, the operator captures.
  4. Replay: the identical action re-invokes AMW with the same key and
     returns the original receipt; the app does NOT create a second
     PayPal order for a key that already has one. Counters stay at 1.
  5. Uncertain outcome: a run can be marked delivery_uncertain instead
     of guessed at; it is never redispatched automatically. The
     reconcile step reads the PayPal order (the external record) and
     settles the state from that.

Configuration (environment variables; see .env.example):
  PAYPAL_SANDBOX_CLIENT_ID / PAYPAL_SANDBOX_SECRET  (PayPal leg; without
      them the app runs in an AMW-only preview and says so — it never
      fabricates a PayPal order)
  PAYPAL_SANDBOX_BASE   (default https://api-m.sandbox.paypal.com)
  AMW_BASE_URL          (default the staging gateway)
  AMW_API_KEY           (or AMW_API_KEY_FILE pointing at a chmod-600 file)
  AMW_WALLET_ID / AMW_PERMIT_ID
  PORT / HOST           (defaults 8000 / 127.0.0.1)
  DATA_DIR              (default ./data — run records as JSON)

Standard library only. No credentials are hardcoded or logged.
"""

import hashlib
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from agent import propose
from amw_client import AMWClient, AMWError
from paypal_client import PayPalClient, PayPalError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
RUNS_FILE = os.path.join(DATA_DIR, "runs.json")

AMW_WALLET_ID = os.environ.get("AMW_WALLET_ID", "agt-6d748f3a0fb7")
AMW_PERMIT_ID = os.environ.get("AMW_PERMIT_ID", "permit-37b1597b97894ccd")
# The one tool the demo permit allows on this allocation. A purchase is
# governed through this tool; asking for any other tool is out of scope
# for the permit and is denied by the gateway with no charge.
PURCHASE_TOOL = os.environ.get("AMW_PURCHASE_TOOL", "partner.echo")

amw = AMWClient()
paypal = PayPalClient()

_lock = threading.Lock()
_runs = {}          # run_id -> run dict
_by_key = {}        # idempotency_key -> run_id


def _persist():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RUNS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"runs": _runs}, f, indent=2)
    os.replace(tmp, RUNS_FILE)


def _load():
    global _runs, _by_key
    if os.path.exists(RUNS_FILE):
        with open(RUNS_FILE) as f:
            _runs = json.load(f).get("runs", {})
        _by_key = {r["idempotency_key"]: rid for rid, r in _runs.items()}


def idempotency_key_for(action):
    canonical = json.dumps(action, sort_keys=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"charge-it-once-{digest}"


def permit_snapshot():
    if not amw.configured:
        return {"configured": False}
    try:
        status, permit = amw.permit(AMW_PERMIT_ID)
    except AMWError as e:
        return {"configured": True, "error": str(e)}
    if status != 200:
        return {"configured": True, "error": f"permit read -> HTTP {status}"}
    return {
        "configured": True,
        "permit_id": permit.get("permit_id"),
        "status": permit.get("status"),
        "allowed_tools": permit.get("allowed_tools"),
        "scopes": permit.get("scopes"),
        "max_credits": permit.get("max_credits"),
        "spent_credits": permit.get("spent_credits"),
        "expires_at": permit.get("expires_at"),
        "subject_wallet_id": permit.get("subject_wallet_id"),
    }


def counters():
    ledger_ids = set()
    order_ids = set()
    charged = 0.0
    for r in _runs.values():
        rec = r.get("receipt") or {}
        if rec.get("ledger_entry_id"):
            if rec["ledger_entry_id"] not in ledger_ids:
                charged += float(rec.get("credits_charged") or 0)
            ledger_ids.add(rec["ledger_entry_id"])
        if r.get("paypal_order_id"):
            order_ids.add(r["paypal_order_id"])
    return {
        "runs": len(_runs),
        "ledger_debits": len(ledger_ids),
        "paypal_orders": len(order_ids),
        "credits_charged_total": charged,
    }


def public_run(run):
    return {k: v for k, v in run.items()}


def do_propose(body):
    proposal = propose(body.get("request_text", ""), body.get("item"),
                       body.get("amount"))
    action = {
        "tool": "paypal.purchase",
        "item": proposal["item"],
        "amount": proposal["amount"],
        "currency": proposal["currency"],
        "wallet_id": AMW_WALLET_ID,
        "permit_id": AMW_PERMIT_ID,
    }
    return {"proposal": proposal,
            "idempotency_key": idempotency_key_for(action),
            "permit": permit_snapshot()}


def _amw_invoke(run):
    """Run (or re-run) the governed invoke for a run. Returns receipt info."""
    status, payload, receipt, is_error = amw.invoke(
        run["tool"], AMW_WALLET_ID, AMW_PERMIT_ID, run["idempotency_key"],
        {"message": run["echo_message"]},
        request_id=f"cio-{uuid.uuid4().hex[:12]}")
    return status, payload, receipt, is_error


def do_run(body):
    proposal = propose(body.get("request_text", ""), body.get("item"),
                       body.get("amount"))
    tool = body.get("tool") or PURCHASE_TOOL
    action = {
        "tool": "paypal.purchase",
        "item": proposal["item"],
        "amount": proposal["amount"],
        "currency": proposal["currency"],
        "wallet_id": AMW_WALLET_ID,
        "permit_id": AMW_PERMIT_ID,
    }
    key = idempotency_key_for(action)
    with _lock:
        if key in _by_key:
            run = _runs[_by_key[key]]
            return {"run": public_run(run), "note":
                    "This exact action already ran under this idempotency "
                    "key. Use Replay to watch the gateway return the "
                    "original receipt instead of charging again."}
        run = {
            "run_id": f"run-{uuid.uuid4().hex[:10]}",
            "idempotency_key": key,
            "proposal": proposal,
            "tool": tool,
            "echo_message": (f"paypal purchase: {proposal['item']} "
                             f"{proposal['amount']:.2f} {proposal['currency']}"),
            "simulate_uncertain": bool(body.get("simulate_uncertain")),
            "status": "created",
            "receipt": None,
            "paypal_order_id": None,
            "paypal_approve_url": None,
            "paypal_capture_id": None,
            "events": ["proposal accepted by operator"],
        }
        _runs[run["run_id"]] = run
        _by_key[key] = run["run_id"]

        # --- AMW leg: authorize + debit + dispatch claim, exactly once ---
        if not amw.configured:
            run["status"] = "amw_not_configured"
            run["events"].append(
                "AMW key not configured; authorization leg cannot run. "
                "No PayPal order was created.")
            _persist()
            return {"run": public_run(run)}
        try:
            status, payload, receipt, is_error = _amw_invoke(run)
        except AMWError as e:
            run["status"] = "amw_error"
            run["events"].append(f"AMW invoke failed: {e}")
            _persist()
            return {"run": public_run(run)}
        if status != 200 or not receipt or is_error or \
                receipt.get("outcome") != "success":
            run["status"] = "denied"
            run["receipt"] = receipt
            run["events"].append(
                "Authorization denied by the gateway (tool outside the "
                "permit's scope, or permit not active). Denied calls are "
                "never charged, and PayPal was not called.")
            _persist()
            return {"run": public_run(run)}
        run["receipt"] = receipt
        run["events"].append(
            f"AMW authorized once: receipt {receipt.get('receipt_id')}, "
            f"ledger entry {receipt.get('ledger_entry_id')}, "
            f"credits charged {receipt.get('credits_charged')}.")

        # --- PayPal leg: only after authorization succeeded ---
        if not paypal.configured:
            run["status"] = "amw_authorized_paypal_not_configured"
            run["events"].append(
                "PayPal Sandbox credentials are not configured, so no "
                "PayPal order was created. The authorization leg above "
                "is real; set the PayPal env vars to enable the payment "
                "leg. Nothing about PayPal is simulated here.")
            _persist()
            return {"run": public_run(run)}
        try:
            order, approve_url = paypal.create_order(
                proposal["amount"], proposal["currency"],
                description=f"Charge it once demo item - {proposal['item']}",
                reference_id=run["run_id"])
        except PayPalError as e:
            run["status"] = "paypal_error"
            run["events"].append(f"PayPal order create failed: {e}")
            _persist()
            return {"run": public_run(run)}
        run["paypal_order_id"] = order.get("id")
        run["paypal_approve_url"] = approve_url
        run["status"] = "awaiting_buyer_approval"
        run["events"].append(
            f"PayPal Sandbox order created: {order.get('id')}. Waiting "
            f"for the buyer to approve in Sandbox.")
        _persist()
        return {"run": public_run(run)}


def do_capture(body):
    run = _runs.get(body.get("run_id"))
    if not run:
        return {"error": "unknown run_id"}, 404
    with _lock:
        if run["status"] == "confirmed":
            return {"run": public_run(run),
                    "note": "Already captured; nothing was dispatched again."}
        if not run.get("paypal_order_id"):
            return {"error": "run has no PayPal order to capture"}, 400
        if run.get("simulate_uncertain") and not run.get("reconciled"):
            # The outcome is treated as unknown on purpose: the state is
            # recorded honestly and NOT redispatched. Reconcile reads the
            # external record (the PayPal order) to settle it.
            run["status"] = "delivery_uncertain"
            run["events"].append(
                "Capture outcome treated as unconfirmed (simulated). The "
                "run is marked delivery_uncertain with its receipt. It "
                "will not be redispatched automatically; use Reconcile.")
            _persist()
            return {"run": public_run(run)}
        try:
            result, capture_id = paypal.capture_order(run["paypal_order_id"])
        except PayPalError as e:
            return {"error": f"capture failed: {e}"}, 502
        if result.get("status") == "COMPLETED" and capture_id:
            run["status"] = "confirmed"
            run["paypal_capture_id"] = capture_id
            run["events"].append(
                f"PayPal order captured: capture {capture_id}, status "
                f"COMPLETED. The order ID is bound into this run's record "
                f"next to the single AMW debit.")
        else:
            run["status"] = "delivery_uncertain"
            run["events"].append(
                f"Capture returned status {result.get('status')} without a "
                f"capture ID; marked delivery_uncertain, not redispatched.")
        _persist()
        return {"run": public_run(run)}


def do_reconcile(body):
    run = _runs.get(body.get("run_id"))
    if not run:
        return {"error": "unknown run_id"}, 404
    with _lock:
        if not run.get("paypal_order_id"):
            return {"error": "run has no PayPal order to reconcile"}, 400
        try:
            order = paypal.get_order(run["paypal_order_id"])
        except PayPalError as e:
            return {"error": f"reconcile read failed: {e}"}, 502
        external = order.get("status")
        run["events"].append(
            f"Reconcile: PayPal's own record for order "
            f"{run['paypal_order_id']} says {external}.")
        if external == "COMPLETED":
            run["status"] = "confirmed"
            try:
                caps = order["purchase_units"][0]["payments"]["captures"]
                run["paypal_capture_id"] = caps[0]["id"]
            except (KeyError, IndexError, TypeError):
                pass
            run["events"].append("External record confirms the payment; "
                                   "state settled as confirmed.")
        elif external == "APPROVED":
            result, capture_id = paypal.capture_order(run["paypal_order_id"])
            if result.get("status") == "COMPLETED" and capture_id:
                run["status"] = "confirmed"
                run["paypal_capture_id"] = capture_id
                run["events"].append(
                    f"External record showed the approved order exactly "
                    f"once; captured it once (capture {capture_id}). No "
                    f"new order was created.")
            run["reconciled"] = True
        else:
            run["events"].append(
                f"External status is {external}; leaving the run as "
                f"{run['status']} until the record settles.")
        _persist()
        return {"run": public_run(run)}


def do_replay(body):
    run = _runs.get(body.get("run_id"))
    if not run:
        return {"error": "unknown run_id"}, 404
    with _lock:
        original = run.get("receipt") or {}
        try:
            status, payload, receipt, is_error = _amw_invoke(run)
        except AMWError as e:
            return {"error": f"replay invoke failed: {e}"}, 502
        same = bool(receipt) and \
            receipt.get("receipt_id") == original.get("receipt_id") and \
            receipt.get("ledger_entry_id") == original.get("ledger_entry_id") and \
            receipt.get("dispatch_attempt_id") == original.get("dispatch_attempt_id")
        run["events"].append(
            "Replay submitted with the same idempotency key: the gateway "
            + ("returned the original receipt." if same else
               "returned a DIFFERENT receipt — that would break the "
               "exactly-once claim and is reported, not hidden.")
            + " No second PayPal order was created for this key.")
        _persist()
        return {"run": public_run(run),
                "replay": {
                    "http_status": status,
                    "receipt_same_as_original": same,
                    "receipt_id": receipt.get("receipt_id") if receipt else None,
                    "ledger_entry_id": receipt.get("ledger_entry_id") if receipt else None,
                    "dispatch_attempt_id": receipt.get("dispatch_attempt_id") if receipt else None,
                    "credits_charged_on_receipt": receipt.get("credits_charged") if receipt else None,
                    "paypal_order_created": False,
                },
                "counters": counters()}


def receipt_view(run):
    rec = run.get("receipt")
    if not rec:
        return None
    live = amw.receipt_by_id(rec["receipt_id"]) if amw.configured else None
    binding = {
        "receipt_id": rec.get("receipt_id"),
        "ledger_entry_id": rec.get("ledger_entry_id"),
        "idempotency_record_id": rec.get("idempotency_record_id"),
        "dispatch_attempt_id": rec.get("dispatch_attempt_id"),
        "credits_charged": rec.get("credits_charged"),
        "outcome": rec.get("outcome"),
        "signature_present": bool(rec.get("signature")),
        "signature_key_id": rec.get("signature_key_id"),
        "matches_live_gateway_record": bool(live) and
            live.get("ledger_entry_id") == rec.get("ledger_entry_id"),
        "paypal_order_id_bound_in_run": run.get("paypal_order_id"),
        "paypal_capture_id_bound_in_run": run.get("paypal_capture_id"),
        "offline_verification": (
            "The receipt is Ed25519-signed. Verify the signature offline "
            "with the standalone receipt verifier and the operator's "
            "public key; the binding fields above are what the signature "
            "covers. This page checks the binding and the live record — "
            "it does not re-verify the signature in the browser."),
    }
    return binding


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Charge it once — Agent Middleware x PayPal Sandbox</title>
<style>
body{background:#f5efe2;color:#111;font-family:Georgia,serif;margin:0;padding:2rem;
max-width:880px;margin-inline:auto}
h1{font-size:2rem;margin-bottom:.2rem} h2{margin-top:2rem}
.card{background:#fffdf6;border:1px solid #111;padding:1rem 1.2rem;margin:1rem 0}
button{background:#111;color:#f5efe2;border:0;padding:.55rem 1rem;cursor:pointer;
font-size:1rem;margin:.2rem .2rem .2rem 0}
input{padding:.5rem;font-size:1rem;border:1px solid #111;background:#fff}
pre{background:#111;color:#f5efe2;padding:1rem;overflow:auto;font-size:.82rem}
.mono{font-family:ui-monospace,Menlo,monospace}
.big{font-size:1.6rem}
.warn{border-left:6px solid #b00;padding-left:.8rem}
</style></head><body>
<h1>Charge it once</h1>
<p><i>Authorize one agent action. Charge it once. Prove what happened.</i></p>
<p>An agent proposes a purchase. It cannot authorize it. The purchase is
governed by an operator-issued permit through Agent Middleware: one
accepted idempotency key means at most one dispatch and at most one
ledger debit — never a duplicate charge, not always a charge. Only then
does a PayPal Sandbox order exist. Replay the action and watch the
counters refuse to move.</p>
<div class="card" id="config">Loading configuration…</div>

<h2>1 · The agent proposes</h2>
<div class="card">
<input id="req" size="52" value="Buy a demo notebook for $1.00">
<button onclick="propose()">Propose</button>
<pre id="proposal">No proposal yet.</pre>
</div>

<h2>2 · The operator approves the run</h2>
<div class="card">
<button onclick="run()">Approve &amp; run (authorize → debit → PayPal order)</button>
<button onclick="runOutOfScope()">Try an out-of-scope tool (denial)</button>
<label><input type="checkbox" id="uncertain"> simulate an unconfirmed capture outcome</label>
<pre id="runout">No run yet.</pre>
<p id="approve"></p>
<button onclick="capture()">Buyer approved → capture</button>
<button onclick="reconcile()">Reconcile against PayPal's record</button>
</div>

<h2>3 · The replay — try to charge it twice</h2>
<div class="card">
<button onclick="replay()">Replay the identical action, same key</button>
<pre id="replay">No replay yet.</pre>
</div>

<h2>4 · Counters and the receipt</h2>
<div class="card">
<p class="big">Ledger debits: <span id="c-debits">–</span> ·
PayPal orders: <span id="c-orders">–</span> ·
Credits charged: <span id="c-credits">–</span></p>
<button onclick="showReceipt()">Show the receipt &amp; its binding</button>
<pre id="receipt">No receipt yet.</pre>
</div>

<script>
let currentRun = null;
async function j(method, path, body) {
  const r = await fetch(path, {method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined});
  return r.json();
}
function show(id, obj) { document.getElementById(id).textContent =
  typeof obj === "string" ? obj : JSON.stringify(obj, null, 2); }
async function refresh() {
  const s = await j("GET", "/api/state");
  document.getElementById("config").innerHTML =
    "<b>Permit:</b> <span class='mono'>" + JSON.stringify(s.permit) + "</span><br>" +
    "<b>PayPal Sandbox:</b> " + (s.paypal_configured ? "configured" :
      "<span class='warn'>not configured — the AMW leg is live, the PayPal leg is disabled, and no PayPal order will be fabricated.</span>");
  document.getElementById("c-debits").textContent = s.counters.ledger_debits;
  document.getElementById("c-orders").textContent = s.counters.paypal_orders;
  document.getElementById("c-credits").textContent = s.counters.credits_charged_total;
}
async function propose() {
  const p = await j("POST", "/api/propose", {request_text: document.getElementById("req").value});
  show("proposal", p);
}
async function runWith(extra) {
  const body = Object.assign({request_text: document.getElementById("req").value,
    simulate_uncertain: document.getElementById("uncertain").checked}, extra || {});
  const r = await j("POST", "/api/run", body);
  if (r.run) { currentRun = r.run; }
  show("runout", r);
  const a = document.getElementById("approve");
  if (currentRun && currentRun.paypal_approve_url) {
    a.innerHTML = "Buyer step: <a href='" + currentRun.paypal_approve_url +
      "' target='_blank'>approve this order in PayPal Sandbox</a>, then come back and capture.";
  } else { a.textContent = ""; }
  refresh();
}
async function run() { runWith({}); }
async function runOutOfScope() { runWith({tool: "partner.other"}); }
async function capture() {
  if (!currentRun) return show("runout", "Run something first.");
  const r = await j("POST", "/api/capture", {run_id: currentRun.run_id});
  if (r.run) currentRun = r.run;
  show("runout", r); refresh();
}
async function reconcile() {
  if (!currentRun) return show("runout", "Run something first.");
  const r = await j("POST", "/api/reconcile", {run_id: currentRun.run_id});
  if (r.run) currentRun = r.run;
  show("runout", r); refresh();
}
async function replay() {
  if (!currentRun) return show("replay", "Run something first.");
  const r = await j("POST", "/api/replay", {run_id: currentRun.run_id});
  show("replay", r); refresh();
}
async function showReceipt() {
  if (!currentRun) return show("receipt", "Run something first.");
  const r = await j("GET", "/api/receipt?run_id=" + currentRun.run_id);
  show("receipt", r);
}
refresh();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, ctype="application/json"):
        if ctype == "application/json":
            data = json.dumps(obj, indent=2).encode()
        else:
            data = obj.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE, "text/html")
        elif parsed.path == "/api/state":
            self._send(200, {
                "permit": permit_snapshot(),
                "paypal_configured": paypal.configured,
                "amw_configured": amw.configured,
                "counters": counters(),
                "runs": [public_run(r) for r in _runs.values()],
            })
        elif parsed.path == "/api/receipt":
            from urllib.parse import parse_qs
            run_id = parse_qs(parsed.query).get("run_id", [None])[0]
            run = _runs.get(run_id)
            if not run:
                self._send(404, {"error": "unknown run_id"})
            else:
                self._send(200, receipt_view(run) or {"error": "run has no receipt"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self._body()
        handlers = {
            "/api/propose": do_propose,
            "/api/run": do_run,
            "/api/capture": do_capture,
            "/api/replay": do_replay,
            "/api/reconcile": do_reconcile,
        }
        fn = handlers.get(parsed.path)
        if fn is None:
            self._send(404, {"error": "not found"})
            return
        out = fn(body)
        obj, code = out if isinstance(out, tuple) else (out, 200)
        self._send(code, obj)


def main():
    _load()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Charge it once listening on http://{host}:{port}")
    print(f"AMW configured: {amw.configured} | PayPal configured: {paypal.configured}")
    server.serve_forever()


if __name__ == "__main__":
    main()
