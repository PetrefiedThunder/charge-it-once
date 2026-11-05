"""The proposing agent for the "Charge it once" prototype.

The agent's job in the flow is narrow and explicit: read a purchase
request, form a structured purchase proposal (item, amount, currency,
payee), and explain the proposal in plain language. The agent cannot
authorize anything: it cannot mint a permit, raise a budget, or reach
PayPal. Authorization happens only in the Agent Middleware leg, under an
operator-issued permit.

This build ships a small deterministic proposer behind the propose()
interface so the demo runs with no extra model keys. A hosted deployment
can swap in an AI model inside propose() without changing the flow: the
contract is (request text) -> (structured proposal + explanation). The
submission's Tools disclosure must describe whichever proposer is
actually deployed; do not describe this stand-in as an AI model.
"""

import re

DEMO_PAYEE = "PayPal Sandbox merchant (demo)"


def _parse_amount(text):
    m = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def propose(request_text="", item=None, amount=None):
    """Return a structured purchase proposal dict."""
    text = (request_text or "").strip()
    if amount is None:
        amount = _parse_amount(text)
    if not item:
        # Take the words after "buy" if present, minus any price fragment.
        m = re.search(r"\bbuy\b\s+(?:a\s+|an\s+|the\s+)?(.+)", text, re.I)
        candidate = m.group(1) if m else text
        candidate = re.sub(r"\$?\s*\d+(?:\.\d{1,2})?\s*(?:usd|dollars?)?", "",
                           candidate, flags=re.I).strip(" .,-")
        item = candidate or "Demo item"
    if amount is None:
        amount = 1.00
    amount = round(float(amount), 2)
    rationale = (
        f"You asked for: {text or item}. I am proposing one purchase of "
        f"'{item}' for ${amount:.2f} USD, paid to {DEMO_PAYEE} through "
        f"PayPal Sandbox. I can propose this, but I cannot authorize it: "
        f"the charge only happens if the operator-issued permit accepts "
        f"the action, and it can be charged at most once for this action."
    )
    return {
        "item": item,
        "amount": amount,
        "currency": "USD",
        "payee": DEMO_PAYEE,
        "rationale": rationale,
    }
