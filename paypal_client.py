"""PayPal Sandbox client for the "Charge it once" prototype.

Uses exactly the Orders API calls the Gate 1 script proved on 2026-11-04
(gate1-sandbox-roundtrip/gate1_paypal_roundtrip.py, Gate 1 PASS: a $1.00
sandbox order created, buyer-approved, and captured to COMPLETED):

  POST /v1/oauth2/token            (client-credentials, form encoded)
  POST /v2/checkout/orders         (intent CAPTURE, payer-action link)
  POST /v2/checkout/orders/{id}/capture
  GET  /v2/checkout/orders/{id}    (used for reconciliation)

Credentials come from the environment only:
  PAYPAL_SANDBOX_CLIENT_ID
  PAYPAL_SANDBOX_SECRET
Optional: PAYPAL_SANDBOX_BASE (defaults to https://api-m.sandbox.paypal.com;
tests point it at a local stub that implements the same shapes).

Standard library only.
"""

import base64
import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class PayPalClient:
    def __init__(self, client_id=None, secret=None, base=None):
        self.client_id = client_id if client_id is not None else os.environ.get(
            "PAYPAL_SANDBOX_CLIENT_ID")
        self.secret = secret if secret is not None else os.environ.get(
            "PAYPAL_SANDBOX_SECRET")
        self.base = (base or os.environ.get("PAYPAL_SANDBOX_BASE")
                     or DEFAULT_BASE).rstrip("/")

    @property
    def configured(self):
        return bool(self.client_id and self.secret)

    def _call(self, method, path, token=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return resp.status, json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise PayPalError(f"{method} {path} -> HTTP {e.code}: {detail}",
                              status=e.code)
        except urllib.error.URLError as e:
            raise PayPalError(f"{method} {path} -> network error: {e.reason}")

    def access_token(self):
        if not self.configured:
            raise PayPalError(
                "PAYPAL_SANDBOX_CLIENT_ID and PAYPAL_SANDBOX_SECRET are not "
                "set; no network call was made.")
        url = self.base + "/v1/oauth2/token"
        req = urllib.request.Request(url, data=b"grant_type=client_credentials",
                                     method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        raw = base64.b64encode(
            f"{self.client_id}:{self.secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {raw}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())["access_token"]
        except urllib.error.HTTPError as e:
            raise PayPalError(
                f"OAuth token request -> HTTP {e.code}: "
                f"{e.read().decode(errors='replace')}", status=e.code)
        except urllib.error.URLError as e:
            raise PayPalError(f"OAuth token request -> network error: {e.reason}")

    def create_order(self, amount, currency="USD", description="",
                     reference_id="charge-it-once-item"):
        """Same create body shape as the Gate 1 script."""
        token = self.access_token()
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference_id,
                    "description": description,
                    "amount": {"currency_code": currency,
                               "value": f"{float(amount):.2f}"},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                        "user_action": "PAY_NOW",
                    }
                }
            },
        }
        _, order = self._call("POST", "/v2/checkout/orders", token=token, body=body)
        approve_url = None
        for link in order.get("links", []):
            if link.get("rel") in ("payer-action", "approve"):
                approve_url = link.get("href")
                break
        return order, approve_url

    def capture_order(self, order_id):
        token = self.access_token()
        _, result = self._call(
            "POST", f"/v2/checkout/orders/{order_id}/capture",
            token=token, body={})
        capture_id = None
        try:
            capture_id = result["purchase_units"][0]["payments"]["captures"][0]["id"]
        except (KeyError, IndexError, TypeError):
            pass
        return result, capture_id

    def get_order(self, order_id):
        token = self.access_token()
        _, result = self._call("GET", f"/v2/checkout/orders/{order_id}",
                               token=token)
        return result
