"""The enforcement pipeline (Phase 2: steps 0-4, 8, 9, 11, 13, 15, 18)."""
import json
import time
import uuid

from pydantic import ValidationError

from common.envelope import PlanEntry, ToolCall
from gateway.identity import IdentityError
from gateway.llm import LLMError
from gateway.policy import PolicyBlock
from gateway.tools_client import ToolsUnavailable

REAL_TOOLS = {"get_customer", "get_payments", "issue_refund", "send_email", "update_record"}
SIDE_EFFECT_TOOLS = {"issue_refund", "send_email", "update_record"}
PSEUDO_TOOLS = {"llm", "submit_plan", "report_result"}
SYSTEM_TOOLS = {"reverse_refund", "recall_email"}


def _error(state, call: ToolCall, code: int, reason: str, event_type: str = "tool_error") -> tuple[int, dict]:
    """Record an error event and build the failure response."""
    state.store.add_event(event_type, call.run_id, call.step_id, call.agent_id, call.tool, {"reason": reason})
    return code, {"ok": False, "error": reason}


def _exec_pseudo(state, call: ToolCall, role: str) -> tuple[int, dict | str]:
    """Run llm / submit_plan / report_result; return (status, raw_text or error body)."""
    if call.tool == "llm":
        try:
            return 200, json.dumps({"result": state.llm.complete(role, call.args)})
        except LLMError as e:
            return 502, str(e)
    if call.tool == "submit_plan":
        try:
            plan = [PlanEntry(**p).model_dump() for p in call.args["plan"]]
        except (ValidationError, KeyError, TypeError):
            return 403, "invalid_args"
        state.store.set_run_plan(call.run_id, plan)
        state.store.add_event("plan_submitted", call.run_id, call.step_id, call.agent_id, call.tool,
                              {"entries": len(plan)})
        return 200, json.dumps({"result": {"stored": len(plan)}})
    ok = bool(call.args.get("ok"))
    state.store.finish_run(call.run_id, "completed" if ok else "failed")
    state.store.add_event("run_completed" if ok else "run_failed", call.run_id, call.step_id,
                          call.agent_id, call.tool, {"detail": call.args.get("detail", "")})
    return 200, json.dumps({"result": {"recorded": True}})


def _exec_real(state, call: ToolCall) -> tuple[int, str, tuple[int, dict] | None]:
    """Forward to the tools service; return (status, raw_text, error_response_or_None)."""
    try:
        status, text = state.tools.call(call.tool, call.args)
    except ToolsUnavailable:
        return 0, "", _error(state, call, 503, "tool_unavailable")
    if status == 503:
        return status, text, _error(state, call, 503, "tool_unavailable")
    if status != 200:
        return status, text, _error(state, call, 502, "tool_error")
    return status, text, None


def _block(state, call: ToolCall, code: int, reason: str, event_type: str) -> tuple[int, dict]:
    """Record a block event and build the blocked response."""
    state.store.add_event(event_type, call.run_id, call.step_id, call.agent_id, call.tool, {"reason": reason})
    return code, {"ok": False, "blocked": True, "reason": reason, "event_type": event_type}


def _execute(state, call: ToolCall, role: str, on: bool, t0: float) -> tuple[int, dict]:
    """Steps 8-18: re-check kill-switch, execute, parse, record, store idempotency."""
    store = state.store
    if on and call.tool in SIDE_EFFECT_TOOLS:
        reason = state.killswitch.check(store.get_agent(call.agent_id), call.tool)
        if reason:
            return _block(state, call, 403, reason, "agent_not_active")
    if call.tool in PSEUDO_TOOLS:
        status, raw = _exec_pseudo(state, call, role)
        if status == 403:
            return _block(state, call, 403, raw, "guard_blocked")
        if status != 200:
            return _error(state, call, 502, "llm_error")
    elif call.tool in REAL_TOOLS:
        status, raw, err = _exec_real(state, call)
        if err:
            return err
    else:
        return _error(state, call, 502, "tool_error")

    try:
        result = json.loads(raw)["result"]
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        if on:
            return _error(state, call, 502, "invalid_tool_output")
        result = {"raw": raw}

    side_effect = 1 if call.tool in SIDE_EFFECT_TOOLS else 0
    store.add_step({"step_id": call.step_id, "run_id": call.run_id, "agent_id": call.agent_id, "role": role,
                    "tool": call.tool, "args": call.args, "result": result,
                    "parent_step_ids": call.parent_step_ids, "plan_index": None, "deviation": 0,
                    "side_effect": side_effect, "tainted": 0, "ts": time.time()})
    store.add_event("tool_called", call.run_id, call.step_id, call.agent_id, call.tool,
                    {"status_code": 200, "latency_ms": (time.time() - t0) * 1000})
    store.add_event("checkpoint_saved", call.run_id, call.step_id, call.agent_id, call.tool)
    if side_effect:
        store.add_event("side_effect", call.run_id, call.step_id, call.agent_id, call.tool)
    response = {"ok": True, "step_id": call.step_id, "result": result}
    if on and call.idempotency_key:
        store.put_idempotency(call.idempotency_key, call.run_id, call.step_id, 200, response)
    return 200, response


def decide_approval(state, approval_id: str, decision: str, reason: str = "approval_rejected") -> None:
    """Approve (execute the stored call) or reject a pending approval; no-op if not pending."""
    store = state.store
    if not store.claim_approval(approval_id, "approved" if decision == "approve" else "rejected"):
        return
    ap = store.get_approval(approval_id)
    call = ToolCall(**ap["envelope"])
    if decision == "approve":
        store.add_event("approval_decided", call.run_id, call.step_id, call.agent_id, call.tool,
                        {"decision": "approved", "approval_id": approval_id})
        agent = store.get_agent(call.agent_id)
        code, resp = _execute(state, call, agent["role"] if agent else "unknown", True, time.time())
    else:
        store.add_event("approval_decided", call.run_id, call.step_id, call.agent_id, call.tool,
                        {"decision": "rejected", "reason": reason, "approval_id": approval_id})
        code, resp = _block(state, call, 403, reason, "policy_blocked")
    store.set_approval_response(approval_id, code, resp)


def expire_approval(state, approval_id: str) -> None:
    """Reject a pending approval older than the timeout (lazy check)."""
    ap = state.store.get_approval(approval_id)
    if ap and ap["status"] == "pending" and time.time() - ap["created_at"] > state.policy.approval_timeout:
        decide_approval(state, approval_id, "reject", "approval_timeout")


def _request_approval(state, call: ToolCall) -> tuple[int, dict]:
    """Create an approval; auto modes decide immediately, manual returns 202."""
    store = state.store
    approval_id = uuid.uuid4().hex[:12]
    store.add_approval(approval_id, call.model_dump())
    store.add_event("approval_requested", call.run_id, call.step_id, call.agent_id, call.tool,
                    {"approval_id": approval_id})
    mode = store.get_config("approval_mode")
    if mode not in ("auto_approve", "auto_reject"):
        return 202, {"ok": False, "pending_approval": True, "approval_id": approval_id}
    decide_approval(state, approval_id, "approve" if mode == "auto_approve" else "reject")
    ap = store.get_approval(approval_id)
    return ap["status_code"], ap["response"]


def handle_call(state, authorization: str | None, body: dict) -> tuple[int, dict]:
    """Process one /call request through the enforcement chain."""
    try:
        call = ToolCall(**body)
    except ValidationError:
        return 400, {"ok": False, "error": "bad_envelope"}
    t0 = time.time()
    store = state.store
    on = store.get_config("mode") == "on"
    store.add_event("call_received", call.run_id, call.step_id, call.agent_id, call.tool)
    agent = store.get_agent(call.agent_id)

    if on:
        try:
            agent = state.identity.verify_call(authorization, body)
        except IdentityError as e:
            event = "replay_blocked" if e.reason == "replay_detected" else "identity_failed"
            return _block(state, call, 401, e.reason, event)
        reason = state.killswitch.check(agent, call.tool)
        if reason:
            return _block(state, call, 403, reason, "agent_not_active")
        stored = store.get_idempotency(call.idempotency_key) if call.idempotency_key else None
        if stored:
            store.add_event("idempotent_replay", call.run_id, call.step_id, call.agent_id, call.tool)
            return stored["status_code"], stored["response"]
        blocked = state.policy.check(agent["role"], call, store.get_run(call.run_id))
        if isinstance(blocked, PolicyBlock):
            return _block(state, call, 403, blocked.reason, "policy_blocked")
        if state.policy.requires_approval(call):
            return _request_approval(state, call)
    return _execute(state, call, agent["role"] if agent else "unknown", on, t0)
