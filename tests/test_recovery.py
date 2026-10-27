from agents.workflow import Behaviour, run_workflow
from common.envelope import RunRequest, canonical_plan
from scenarios.runner import evaluate, load_scenario

ROLES = ["planner", "retriever", "executor", "auditor"]


def _req(run_id: str) -> RunRequest:
    return RunRequest(run_id=run_id, scenario="t", customer_id="C001", amount=49.99, reason="duplicate charge")


def test_deviation_detected(admin, make_agent):
    agents = {r: make_agent(r) for r in ROLES}
    admin.create_run(_req("dev2"))
    agents["planner"].call("submit_plan", {"plan": canonical_plan("C001", 49.99)}, "dev2")
    cust = agents["retriever"].call("get_customer", {"customer_id": "C001"}, "dev2")
    assert cust.ok
    agents["executor"].call("update_record", {"customer_id": "C001", "field": "notes", "value": "x"}, "dev2")
    steps = {s["tool"]: s for s in admin.steps("dev2")}
    assert steps["get_customer"]["plan_index"] == 0
    assert steps["update_record"]["deviation"] == 1
    assert any(e["type"] == "plan_deviation" for e in admin.events(run_id="dev2"))


def test_compromise_quarantine_and_rollback(admin, make_agent):
    agents = {r: make_agent(r) for r in ROLES}
    count = {"executor": 1}

    def replace(role: str):
        count[role] = count.get(role, 1) + 1
        return make_agent(role, f"{role}-{count[role]}")

    outcome = run_workflow(admin, agents, _req("comp1"), Behaviour(executor_mode="compromise"),
                           make_agent=replace)
    assert outcome.status == "completed"
    assert outcome.recoveries == 1
    detail, passed = evaluate(load_scenario("compromised_agent")["expect"], admin.tool_state(), "C001",
                              outcome.status, None)
    assert passed, detail
    events = admin.events(run_id="comp1")
    assert [e["detail"]["agent_id"] for e in events if e["type"] == "rollback_started"] == ["executor-1"]
    assert any(e["type"] == "compensation_applied" for e in events)
    assert {a["agent_id"]: a["status"] for a in admin.agents()}["executor-1"] == "quarantined"


def test_quarantine_events_carry_run_id(admin, make_agent):
    agents = {r: make_agent(r) for r in ROLES}
    run_workflow(admin, agents, _req("comp2"), Behaviour(executor_mode="compromise"),
                 make_agent=lambda role: make_agent(role, f"{role}-2"))
    events = admin.events(run_id="comp2")
    quarantined = [e for e in events if e["type"] == "agent_quarantined"]
    assert [e["agent_id"] for e in quarantined] == ["executor-1"]
    assert any(e["type"] == "anomaly_detected" and e["detail"].get("kind") == "monitor_rule" for e in events)
