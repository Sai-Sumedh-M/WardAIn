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
