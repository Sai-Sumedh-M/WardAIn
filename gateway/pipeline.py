"""The enforcement pipeline (steps 0-18)."""
import json
import time
import uuid

from pydantic import ValidationError

from common.envelope import PlanEntry, ToolCall
from gateway import guards
from gateway.identity import IdentityError
from gateway.llm import LLMError
from gateway.nvidia_guard import GuardError
from gateway.policy import PolicyBlock
from gateway.recovery import undo_for
from gateway.tools_client import ToolsUnavailable

REAL_TOOLS = {"get_customer", "get_payments", "issue_refund", "send_email", "update_record"}
SIDE_EFFECT_TOOLS = {"issue_refund", "send_email", "update_record"}
PSEUDO_TOOLS = {"llm", "submit_plan", "report_result"}
SYSTEM_TOOLS = {"reverse_refund", "recall_email"}


def _error(state, call: ToolCall, code: int, reason: str, event_type: str = "tool_error") -> tuple[int, dict]:
    """Record an error event and build the failure response."""
    state.store.add_event(event_type, call.run_id, call.step_id, call.agent_id, call.tool, {"reason": reason})
    return code, {"ok": False, "error": reason}


def _sanitized(state, call: ToolCall, kind_field: str, layer: str = "regex") -> None:
    """Emit the injection_sanitized and anomaly_detected events for one sanitised field."""
    state.store.add_event("injection_sanitized", call.run_id, call.step_id, call.agent_id, call.tool,
                          {"field": kind_field, "layer": layer})
    state.store.add_event("anomaly_detected", call.run_id, call.step_id, call.agent_id, call.tool,
                          {"kind": "prompt_injection", "field": kind_field, "layer": layer})


def _nvidia_sanitize(state, call: ToolCall, result: dict, regex_fields: list[str]) -> dict:
    """Run the NVIDIA guard layers over long string fields the regex did not already replace."""
    mode = state.store.get_config("nvidia_guard")
    if mode == "off":
        return result
    out = dict(result)
    for f, v in result.items():
        if f in regex_fields or not isinstance(v, str) or len(v) < 20:
            continue
        try:
            flagged, layer = state.nvidia_guard.check(v, mode)
        except GuardError as e:
            state.store.add_event("guard_unavailable", call.run_id, call.step_id, call.agent_id, call.tool,
                                  {"layer": mode, "error": str(e), "field": f})
            flagged, layer = state.settings.NVIDIA_GUARD_FAIL == "closed", "fail_closed"
        if flagged:
            out[f] = guards.REMOVED
            _sanitized(state, call, f, layer)
    return out


def _nvidia_review(state, call: ToolCall, result: dict, regex_fields: list[str]) -> tuple[dict, list[dict]]:
    """HITL variant: both layers vote; both flag -> replace, exactly one -> ambiguous (kept in result)."""
    mode = state.store.get_config("nvidia_guard")
    out, ambiguous = dict(result), []

    def unavailable(layer: str, error: Exception, field: str) -> None:
        state.store.add_event("guard_unavailable", call.run_id, call.step_id, call.agent_id, call.tool,
                              {"layer": layer, "error": str(error), "field": field})

    for f, v in result.items():
        if f in regex_fields or not isinstance(v, str) or len(v) < 20:
            continue
        s, j = state.nvidia_guard.check_both(v, lambda layer, e, f=f: unavailable(layer, e, f))
        if s and j:
            out[f] = guards.REMOVED
            _sanitized(state, call, f, "nvidia_both")
        elif s or j:
            ambiguous.append({"field": f, "text": v, "flagged_by": "nvidia_safety" if s else "nvidia_judge"})
    return out, ambiguous


def _exec_pseudo(state, call: ToolCall, role: str, on: bool) -> tuple[int, dict | str]:
    """Run llm / submit_plan / report_result; return (status, raw_text or error body)."""
    if call.tool == "llm":
        args = call.args
        if on:
            messages, n = guards.sanitize_messages(args["messages"])
            if n:
                _sanitized(state, call, "messages")
            args = {**args, "messages": messages}
        try:
            return 200, json.dumps({"result": state.llm.complete(role, args)})
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


def _exec_real(state, call: ToolCall, on: bool) -> tuple[int, str, tuple[int, dict] | None]:
    """Forward to the tools service; return (status, raw_text, error_response_or_None)."""
    try:
        status, text = state.tools.call(call.tool, call.args)
    except ToolsUnavailable:
        status, text = 0, ""
    if on:
        state.circuit.record(call.tool, 0 < status < 500, call.run_id, call.agent_id)
    if status in (0, 503):
        return status, text, _error(state, call, 503, "tool_unavailable")
    if status != 200:
        return status, text, _error(state, call, 502, "tool_error")
    return status, text, None


def _block(state, call: ToolCall, code: int, reason: str, event_type: str) -> tuple[int, dict]:
    """Record a block event and build the blocked response."""
    state.store.add_event(event_type, call.run_id, call.step_id, call.agent_id, call.tool, {"reason": reason})
    return code, {"ok": False, "blocked": True, "reason": reason, "event_type": event_type}


def _execute(state, call: ToolCall, role: str, on: bool, t0: float) -> tuple[int, dict]:
    """Steps 6-18: chaos, circuit, kill-switch re-check, execute, parse, sanitise, record, idempotency."""
    store = state.store
    if state.chaos.pre(call):
        return _error(state, call, 502, "tool_error")
    if on and call.tool in REAL_TOOLS and state.circuit.is_open(call.tool):
        return _error(state, call, 503, "circuit_open")
    if on and call.tool in SIDE_EFFECT_TOOLS:
        reason = state.killswitch.check(store.get_agent(call.agent_id), call.tool)
        if reason:
            return _block(state, call, 403, reason, "agent_not_active")
    if call.tool in PSEUDO_TOOLS:
        status, raw = _exec_pseudo(state, call, role, on)
        if status == 403:
            return _block(state, call, 403, raw, "guard_blocked")
        if status != 200:
            return _error(state, call, 502, "llm_error")
    elif call.tool in REAL_TOOLS:
        status, raw, err = _exec_real(state, call, on)
        if err:
            return err
    else:
        return _error(state, call, 502, "tool_error")
    raw, timeout_after_commit = state.chaos.post(call, raw)

    try:
        result = json.loads(raw)["result"]
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        if on:
            return _error(state, call, 502, "invalid_tool_output")
        result = {"raw": raw}

    if on and call.tool in ("get_customer", "get_payments"):
        result, fields = guards.sanitize_output(result)
        for f in fields:
            _sanitized(state, call, f)
        if state.store.get_config("hitl") == "ambiguous" and state.store.get_config("nvidia_guard") != "off":
            result, ambiguous = _nvidia_review(state, call, result, fields)
            if ambiguous:
                return _request_review(state, call, result, ambiguous)
        else:
            result = _nvidia_sanitize(state, call, result, fields)
    return _finish(state, call, role, on, t0, result, timeout_after_commit)


def _finish(state, call: ToolCall, role: str, on: bool, t0: float, result: dict,
            timeout_after_commit: bool) -> tuple[int, dict]:
    """Steps 13-18: record the step, deviation check, idempotency, timeout_after_commit."""
    store = state.store
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
        undo = undo_for(call.tool, result) if on else None
        if undo:
            store.add_compensation(call.run_id, call.step_id, call.tool, undo[0], undo[1])
    if on:
        state.recovery.check_deviation({"step_id": call.step_id, "run_id": call.run_id,
                                        "agent_id": call.agent_id, "role": role, "tool": call.tool,
                                        "args": call.args})
    response = {"ok": True, "step_id": call.step_id, "result": result}
    if on and call.idempotency_key:
        store.put_idempotency(call.idempotency_key, call.run_id, call.step_id, 200, response)
    if timeout_after_commit:
        return _error(state, call, 504, "timeout")
    return 200, response


def _decide_review(state, ap: dict, call: ToolCall, decision: str, reason: str, by: str) -> None:
    """Release the original (approve) or sanitized (reject / timeout) result of a data review."""
    store, payload = state.store, ap["payload"]
    release = "release_original" if decision == "approve" else "release_sanitized"
    if release == "release_sanitized":
        layer = "review_timeout" if by == "timeout" else "human_review"
        for item in payload["fields"]:
            store.add_event("injection_sanitized", call.run_id, call.step_id, call.agent_id, call.tool,
                            {"field": item["field"], "layer": layer})
    store.add_event("review_decided", call.run_id, call.step_id, call.agent_id, call.tool,
                    {"decision": release, "by": by, "approval_id": ap["approval_id"]})
    result = payload["result_original"] if decision == "approve" else payload["result_sanitized"]
    agent = store.get_agent(call.agent_id)
    code, resp = _finish(state, call, agent["role"] if agent else "unknown", True, time.time(), result, False)
    store.set_approval_response(ap["approval_id"], code, resp)


def _request_review(state, call: ToolCall, result: dict, ambiguous: list[dict]) -> tuple[int, dict]:
    """Escalate ambiguous fields to a human (data_review approval); auto modes decide immediately."""
    store = state.store
    approval_id = uuid.uuid4().hex[:12]
    sanitized = {**result, **{a["field"]: guards.REMOVED for a in ambiguous}}
    store.add_approval(approval_id, call.model_dump(), "data_review",
                       {"result_original": result, "result_sanitized": sanitized, "fields": ambiguous})
    store.add_event("review_requested", call.run_id, call.step_id, call.agent_id, call.tool,
                    {"approval_id": approval_id,
                     "fields": [{"field": a["field"], "flagged_by": a["flagged_by"]} for a in ambiguous]})
    mode = store.get_config("approval_mode")
    if mode not in ("auto_approve", "auto_reject"):
        return 202, {"ok": False, "pending_approval": True, "approval_id": approval_id}
    decide_approval(state, approval_id, "approve" if mode == "auto_approve" else "reject", by="auto")
    ap = store.get_approval(approval_id)
    return ap["status_code"], ap["response"]


def decide_approval(state, approval_id: str, decision: str, reason: str = "approval_rejected",
                    by: str = "human") -> None:
    """Approve (execute the stored call) or reject a pending approval; no-op if not pending."""
    store = state.store
    if not store.claim_approval(approval_id, "approved" if decision == "approve" else "rejected"):
        return
    ap = store.get_approval(approval_id)
    call = ToolCall(**ap["envelope"])
    if ap["kind"] == "data_review":
        return _decide_review(state, ap, call, decision, reason, by)
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
    if not ap or ap["status"] != "pending":
        return
    limit = state.policy.review_timeout if ap["kind"] == "data_review" else state.policy.approval_timeout
    if time.time() - ap["created_at"] > limit:
        decide_approval(state, approval_id, "reject", "approval_timeout", by="timeout")


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
    decide_approval(state, approval_id, "approve" if mode == "auto_approve" else "reject", by="auto")
    ap = store.get_approval(approval_id)
    return ap["status_code"], ap["response"]


def handle_call(state, authorization: str | None, body: dict) -> tuple[int, dict]:
    """Process one /call request, then run the monitor (step 17) for authenticated calls."""
    code, resp = _handle(state, authorization, body)
    if code != 401 and code != 400:
        state.monitor.after_call(ToolCall(**body), None)
    return code, resp


def _handle(state, authorization: str | None, body: dict) -> tuple[int, dict]:
    """Steps 0-16 and 18 of the enforcement chain."""
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
        reason = (guards.check_args(call.tool, call.args)
                  or guards.check_loop(store, state.policy.cfg["limits"]["loop_threshold"], call.run_id,
                                       call.tool, call.args)
                  or guards.check_pii(call.tool, call.args))
        if reason:
            return _block(state, call, 403, reason, "guard_blocked")
        if state.policy.requires_approval(call):
            return _request_approval(state, call)
    return _execute(state, call, agent["role"] if agent else "unknown", on, t0)
