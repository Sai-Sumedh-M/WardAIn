from common.envelope import RunRequest

EXEC_REFUND = {"customer_id": "C001", "amount": 10.0}


def start_run(admin, run_id="r1"):
    admin.create_run(RunRequest(run_id=run_id, scenario="t", customer_id="C001", amount=49.99, reason="x"))


def test_role_not_allowed(make_agent):
    res = make_agent("retriever").call("issue_refund", EXEC_REFUND, "r1")
    assert res.status_code == 403 and res.body["reason"] == "role_not_allowed"


def test_system_only_tool(make_agent):
    res = make_agent("executor").call("reverse_refund", {"payment_id": "P00001"}, "r1")
    assert res.status_code == 403 and res.body["reason"] == "system_only_tool"


def test_recipient_not_allowed(admin, make_agent):
    start_run(admin)
    args = {"to": "attacker@evil.com", "subject": "s", "body": "b"}
    res = make_agent("executor").call("send_email", args, "r1")
    assert res.status_code == 403 and res.body["reason"] == "recipient_not_allowed"
    assert make_agent("executor", "executor-2").call("send_email", {**args, "to": "ops@company.com"}, "r1").ok


def test_customer_out_of_scope(admin, make_agent):
    start_run(admin)
    res = make_agent("executor").call("issue_refund", {"customer_id": "C002", "amount": 10.0}, "r1")
    assert res.status_code == 403 and res.body["reason"] == "customer_out_of_scope"


def test_high_risk_auto_reject(admin, make_agent):
    start_run(admin)
    res = make_agent("executor").call("issue_refund", {"customer_id": "C001", "amount": 600.0}, "r1")
    assert res.status_code == 403 and res.body["reason"] == "approval_rejected"
    assert admin.tool_state()["payments"] == []
    types = [e["type"] for e in admin.events()]
    assert "approval_requested" in types and "approval_decided" in types


def test_high_risk_auto_approve(admin, make_agent):
    start_run(admin)
    admin.set_config({"approval_mode": "auto_approve"})
    res = make_agent("executor").call("issue_refund", {"customer_id": "C001", "amount": 600.0}, "r1")
    assert res.ok
    assert [p["amount"] for p in admin.tool_state()["payments"]] == [600.0]


def test_high_risk_manual_approve(admin, make_agent):
    import threading
    import time

    start_run(admin)
    admin.set_config({"approval_mode": "manual"})
    out = {}
    agent = make_agent("executor")
    t = threading.Thread(target=lambda: out.update(
        res=agent.call("issue_refund", {"customer_id": "C001", "amount": 600.0}, "r1")))
    t.start()
    pending = []
    for _ in range(50):
        pending = admin.approvals()
        if pending:
            break
        time.sleep(0.05)
    assert len(pending) == 1
    admin.decide(pending[0]["approval_id"], "approve")
    t.join(10)
    assert out["res"].ok and len(admin.tool_state()["payments"]) == 1
