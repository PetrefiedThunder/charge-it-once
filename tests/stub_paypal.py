"""Local stub of the PayPal Sandbox Orders API, for tests only.

Implements exactly the shapes paypal_client.py uses — the same calls the
Gate 1 script proved against the real sandbox on 2026-11-04:

  POST /v1/oauth2/token                  -> {"access_token": ...}
  POST /v2/checkout/orders               -> order CREATED + payer-action link
  GET  /approve?token=<order_id>         -> marks the order APPROVED
                                            (stands in for buyer approval)
  POST /v2/checkout/orders/{id}/capture  -> COMPLETED + capture id
  GET  /v2/checkout/orders/{id}          -> order status

State is in-memory. Start: python3 stub_paypal.py [port]
"""

import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ORDERS = {}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        if parsed.path == "/v1/oauth2/token":
            return self._send(200, {"access_token": "stub-token",
                                    "token_type": "Bearer", "expires_in": 32400})
        if parsed.path == "/v2/checkout/orders":
            body = json.loads(raw or "{}")
            oid = uuid.uuid4().hex[:17].upper()
            unit = body["purchase_units"][0]
            ORDERS[oid] = {"id": oid, "status": "CREATED",
                           "purchase_units": [unit], "intent": body.get("intent")}
            base = f"http://{self.headers['Host']}"
            return self._send(201, {
                "id": oid, "status": "CREATED",
                "links": [{"rel": "payer-action",
                           "href": f"{base}/approve?token={oid}"}]})
        if parsed.path.startswith("/v2/checkout/orders/") and \
                parsed.path.endswith("/capture"):
            oid = parsed.path.split("/")[4]
            order = ORDERS.get(oid)
            if not order:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            if order["status"] != "APPROVED":
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY",
                                        "details": [{"issue": "ORDER_NOT_APPROVED"}]})
            cap_id = uuid.uuid4().hex[:17].upper()
            unit = dict(order["purchase_units"][0])
            unit["payments"] = {"captures": [{
                "id": cap_id, "status": "COMPLETED",
                "amount": unit["amount"], "final_capture": True}]}
            order["status"] = "COMPLETED"
            order["purchase_units"] = [unit]
            return self._send(201, order)
        return self._send(404, {"name": "NOT_FOUND"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/approve":
            oid = parse_qs(parsed.query).get("token", [None])[0]
            if oid in ORDERS:
                ORDERS[oid]["status"] = "APPROVED"
                return self._send(200, {"approved": oid})
            return self._send(404, {"name": "NOT_FOUND"})
        if parsed.path == "/__count":
            return self._send(200, {"orders": len(ORDERS),
                                    "statuses": {k: v["status"]
                                                 for k, v in ORDERS.items()}})
        if parsed.path.startswith("/v2/checkout/orders/"):
            oid = parsed.path.split("/")[4]
            order = ORDERS.get(oid)
            if order:
                return self._send(200, order)
            return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
        return self._send(404, {"name": "NOT_FOUND"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8399
    ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
