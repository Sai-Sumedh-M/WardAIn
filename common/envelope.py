"""Shared pydantic contracts."""
from pydantic import BaseModel


class ToolCall(BaseModel):
    run_id: str
    step_id: str
    agent_id: str
    tool: str
    args: dict
    parent_step_ids: list[str] = []
    idempotency_key: str | None = None
    nonce: str
    timestamp: float
    signature: str = ""


class CallResult(BaseModel):
    status_code: int
    body: dict

    @property
    def ok(self) -> bool:
        """True when the gateway returned 200."""
        return self.status_code == 200


class PlanEntry(BaseModel):
    plan_index: int
    role: str
    tool: str
    args: dict


class RunRequest(BaseModel):
    run_id: str
    scenario: str
    customer_id: str
    amount: float
    reason: str


class Event(BaseModel):
    event_id: str
    ts: float
    run_id: str | None
    step_id: str | None
    agent_id: str | None
    type: str
    tool: str | None
    detail: dict


def canonical_plan(customer_id: str, amount: float) -> list[dict]:
    """The canonical 4-step refund plan for a request."""
    return [
        {"plan_index": 0, "role": "retriever", "tool": "get_customer", "args": {"customer_id": customer_id}},
        {"plan_index": 1, "role": "executor", "tool": "issue_refund",
         "args": {"customer_id": customer_id, "amount": amount}},
        {"plan_index": 2, "role": "executor", "tool": "send_email", "args": {"to": "$customer_email"}},
        {"plan_index": 3, "role": "auditor", "tool": "get_payments", "args": {"customer_id": customer_id}},
    ]
