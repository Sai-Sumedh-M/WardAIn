from agents.workflow import Behaviour, run_workflow
from common.envelope import RunRequest
from scenarios.runner import evaluate, load_scenario


def test_normal_run_completes(admin, make_agent):
    agents = {r: make_agent(r) for r in ["planner", "retriever", "executor", "auditor"]}
    req = RunRequest(run_id="r1", scenario="normal_run", customer_id="C001", amount=49.99,
                     reason="duplicate charge")
    outcome = run_workflow(admin, agents, req, Behaviour())
    assert outcome.status == "completed"
    assert outcome.llm_fallbacks == 0
    expect = load_scenario("normal_run")["expect"]
    detail, passed = evaluate(expect, admin.tool_state(), "C001", outcome.status, None)
    assert passed, detail


def test_runaway_loop_expect_uses_halt_ts():
    """payments_after_halt is computed from tool state: leaked payments fail the expect, none pass."""
    expect = load_scenario("runaway_loop")["expect"]
    cust = [{"customer_id": "C001", "email": "c@x.com", "balance": 1000.0}]
    pay = lambda t: {"customer_id": "C001", "amount": 49.99, "status": "completed", "created_at": t}
    leaked = {"customers": cust, "payments": [pay(1.0), pay(2.0), pay(11.0)], "outbox": []}
    _, passed = evaluate(expect, leaked, "C001", "halted", 10.0)
    assert not passed
    clean = {"customers": cust, "payments": [pay(1.0), pay(2.0)], "outbox": []}
    assert evaluate(expect, clean, "C001", "halted", 10.0)[1]
