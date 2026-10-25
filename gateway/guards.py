"""Pre/post guards: argument schemas, loop detection, PII egress, prompt-injection sanitising."""
import re

from common.crypto import canonical_json
from gateway.store import GatewayStore

REAL_TOOLS = {"get_customer", "get_payments", "issue_refund", "send_email", "update_record"}
CUSTOMER_RE = re.compile(r"^C\d{3}$")
REMOVED = "[REMOVED: untrusted instruction]"
INJECTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"ignore (all )?previous instructions", r"system override", r"disregard (all|the) (previous|above)",
    r"you are now", r"call issue_refund")]
PHONE_RE = re.compile(r"\+?\d[\d\- ]{8,}\d")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _cid(v) -> bool:
    """True for a valid customer id."""
    return isinstance(v, str) and bool(CUSTOMER_RE.match(v))


def _num(v) -> bool:
    """True for an int/float that is not a bool."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(v, lo: int, hi: int) -> bool:
    """True for a str with lo <= len <= hi."""
    return isinstance(v, str) and lo <= len(v) <= hi


def _messages_ok(v) -> bool:
    """True for a list of {"role": str, "content": str}."""
    return isinstance(v, list) and all(isinstance(m, dict) and set(m) == {"role", "content"}
                                       and isinstance(m["role"], str) and isinstance(m["content"], str)
                                       for m in v)


SCHEMAS = {
    "get_customer": {"customer_id": _cid},
    "get_payments": {"customer_id": _cid},
    "issue_refund": {"customer_id": _cid, "amount": lambda v: _num(v) and 0 < v <= 10000},
    "send_email": {"to": lambda v: isinstance(v, str) and "@" in v, "subject": lambda v: _text(v, 1, 200),
                   "body": lambda v: _text(v, 1, 2000)},
    "update_record": {"customer_id": _cid, "field": lambda v: v in {"balance", "notes", "email", "phone"},
                      "value": lambda v: isinstance(v, str) or _num(v)},
    "llm": {"task": lambda v: isinstance(v, str), "messages": _messages_ok},
    "submit_plan": {"plan": lambda v: isinstance(v, list)},
    "report_result": {"ok": lambda v: isinstance(v, bool), "detail": lambda v: isinstance(v, str)},
}


def check_args(tool: str, args: dict) -> str | None:
    """Return invalid_args unless args match the tool schema exactly (all keys, no extras)."""
    schema = SCHEMAS.get(tool)
    if schema is None:
        return None
    if set(args) != set(schema) or not all(check(args[k]) for k, check in schema.items()):
        return "invalid_args"
    return None


def check_loop(store: GatewayStore, threshold: int, run_id: str, tool: str, args: dict) -> str | None:
    """Return loop_detected when the run already has >= threshold identical calls of a real tool."""
    if tool not in REAL_TOOLS:
        return None
    key = canonical_json(args)
    same = [s for s in store.get_steps(run_id) if s["tool"] == tool and canonical_json(s["args"]) == key]
    return "loop_detected" if len(same) >= threshold else None


def check_pii(tool: str, args: dict) -> str | None:
    """Return pii_egress when an external email body carries a phone, card or foreign email address."""
    to = str(args.get("to", ""))
    if tool != "send_email" or to.endswith("@company.com"):
        return None
    body = str(args.get("body", ""))
    if PHONE_RE.search(body) or CARD_RE.search(body):
        return "pii_egress"
    return "pii_egress" if any(m != to for m in EMAIL_RE.findall(body)) else None


def _injected(text: str) -> bool:
    """True if text matches any injection pattern."""
    return any(p.search(text) for p in INJECTION_PATTERNS)


def sanitize_output(result: dict) -> tuple[dict, list[str]]:
    """Replace top-level string values containing injected instructions; return (result, fields)."""
    out, fields = dict(result), []
    for k, v in result.items():
        if isinstance(v, str) and _injected(v):
            out[k] = REMOVED
            fields.append(k)
    return out, fields


def sanitize_messages(messages: list[dict]) -> tuple[list[dict], int]:
    """Replace injected substrings in each message content; return (messages, replacements)."""
    count, out = 0, []
    for m in messages:
        content = m.get("content", "")
        for p in INJECTION_PATTERNS:
            content, n = p.subn(REMOVED, content)
            count += n
        out.append({**m, "content": content})
    return out, count
