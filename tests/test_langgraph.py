import pytest

pytest.importorskip("langgraph")

from agents.langgraph_workflow import run_workflow_langgraph  # noqa: E402
from agents.workflow import Behaviour  # noqa: E402
from common.envelope import RunRequest  # noqa: E402
from scenarios.runner import evaluate, load_scenario  # noqa: E402

ROLES = ["planner", "retriever", "executor", "auditor"]


def _req(run_id: str) -> RunRequest:
    return RunRequest(run_id=run_id, scenario="t", customer_id="C001", amount=49.99, reason="duplicate charge")


def test_langgraph_normal_run_completes(admin, make_agent):
    agents = {r: make_agent(r) for r in ROLES}
    outcome = run_workflow_langgraph(admin, agents, _req("lg1"), Behaviour())
    assert outcome.status == "completed"
    assert outcome.llm_fallbacks == 0
    detail, passed = evaluate(load_scenario("normal_run")["expect"], admin.tool_state(), "C001",
                              outcome.status, None)
    assert passed, detail


def test_langgraph_compromise_recovers(admin, make_agent):
    agents = {r: make_agent(r) for r in ROLES}
    count = {"executor": 1}

    def replace(role: str):
        count[role] = count.get(role, 1) + 1
        return make_agent(role, f"{role}-{count[role]}")

    outcome = run_workflow_langgraph(admin, agents, _req("lg2"), Behaviour(executor_mode="compromise"),
                                     make_agent=replace)
    assert outcome.status == "completed"
    assert outcome.recoveries == 1
    detail, passed = evaluate(load_scenario("compromised_agent")["expect"], admin.tool_state(), "C001",
                              outcome.status, None)
    assert passed, detail
    events = admin.events(run_id="lg2")
    assert [e["detail"]["agent_id"] for e in events if e["type"] == "rollback_started"] == ["executor-1"]
    assert any(e["type"] == "compensation_applied" for e in events)
