"""Agent Middleware client for the "Charge it once" prototype.

Talks to the AMW gateway API surface only (the same surface a design
partner would use):

  GET  /health
  GET  /mcp/tools.json
  POST /mcp/messages            (governed tools/call with wallet, permit,
                                 and idempotency key in mcpContext)
  GET  /v1/permits/{permit_id}
  GET  /v1/me/receipts

Auth: X-API-Key header. The key comes from the AMW_API_KEY environment
variable, or from the file named by AMW_API_KEY_FILE. It is never printed,
logged, or written anywhere by this code.

Verified claim boundary (do not exceed): one accepted idempotency key
yields at most one gateway dispatch and at most one ledger debit. Denied
calls are never charged. A post-dispatch outcome that cannot be confirmed
is a receipted delivery_uncertain state, never an automatic redispatch.
Receipts are Ed25519-signed and offline-verifiable with the standalone
verifier and the operator's public key.

Standard library only.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api-service-staging-staging-fb2e.up.railway.app"


class AMWError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AMWClient:
    def __init__(self, base_url=None, api_key=None):
        self.base = (base_url or os.environ.get("AMW_BASE_URL")
                     or DEFAULT_BASE).rstrip("/")
        if api_key is not None:
            self.api_key = api_key
        elif os.environ.get("AMW_API_KEY"):
            self.api_key = os.environ["AMW_API_KEY"]
        else:
            key_file = os.environ.get("AMW_API_KEY_FILE")
            self.api_key = None
            if key_file and os.path.exists(key_file):
                with open(key_file) as f:
                    self.api_key = f.read().strip()

    @property
    def configured(self):
        return bool(self.api_key)

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            try:
                return e.code, json.loads(detail)
            except ValueError:
                return e.code, {"_error": detail}
        except urllib.error.URLError as e:
            raise AMWError(f"{method} {path} -> network error: {e.reason}")

    def health(self):
        return self._req("GET", "/health")

    def permit(self, permit_id):
        return self._req("GET", f"/v1/permits/{permit_id}")

    def receipts(self):
        return self._req("GET", "/v1/me/receipts")

    def receipt_by_id(self, receipt_id):
        status, payload = self.receipts()
        if status != 200:
            return None
        for r in payload.get("receipts", []):
            if r.get("receipt_id") == receipt_id:
                return r
        return None

    def invoke(self, tool, wallet_id, permit_id, idempotency_key,
               arguments, request_id):
        """One governed tools/call. Returns (http_status, payload, receipt).

        The gateway collapses retries: the same idempotency key returns the
        original receipt, with no second dispatch and no second debit.
        """
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": arguments,
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                },
            },
        }
        status, payload = self._req("POST", "/mcp/messages", body)
        receipt = None
        is_error = None
        try:
            result = payload["result"]
            receipt = result.get("receipt")
            is_error = result.get("isError")
        except (KeyError, TypeError):
            pass
        return status, payload, receipt, is_error
