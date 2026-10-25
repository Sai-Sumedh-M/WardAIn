"""The enforcement pipeline (Phase 1: steps 0, 9, 11, 13, 18 only; behaves as OFF)."""
import json
import time

from pydantic import ValidationError

from common.envelope import PlanEntry, ToolCall
from gateway.llm import LLMError
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


def handle_call(state, authorization: str | None, body: dict) -> tuple[int, dict]:
    """Process one /call request through the enforcement chain."""
    try:
        call = ToolCall(**body)
    except ValidationError:
        return 400, {"ok": False, "error": "bad_envelope"}
    t0 = time.time()
    store = state.store
    store.add_event("call_received", call.run_id, call.step_id, call.agent_id, call.tool)
    agent = store.get_agent(call.agent_id)
    role = agent["role"] if agent else "unknown"

    if call.tool in PSEUDO_TOOLS:
        status, raw = _exec_pseudo(state, call, role)
        if status == 403:
            store.add_event("guard_blocked", call.run_id, call.step_id, call.agent_id, call.tool,
                            {"reason": raw})
            return 403, {"ok": False, "blocked": True, "reason": raw, "event_type": "guard_blocked"}
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
    return 200, {"ok": True, "step_id": call.step_id, "result": result}
