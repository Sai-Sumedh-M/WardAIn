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
    if task == "email_body":
        c = _line_json(text, "EMAIL_CONTEXT: ")
        return (f"Hi {c['name']}, your refund of {float(c['amount']):.2f} for {c['reason']} "
                f"has been processed. Reference {c['payment_id']}.")
    return "OK"
