"""LangGraph engine: runs the same step functions as the plain engine, as graph nodes."""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents.workflow import (RunOutcome, WorkflowContext, handle_recovery, step_audit, step_email,
                             step_plan, step_refund, step_retrieve, to_outcome)


class GraphState(TypedDict):
    ctx: WorkflowContext


NODE_BY_INDEX = {0: "retrieve", 1: "refund", 2: "email", 3: "audit"}


def _wrap(fn):
    """Wrap a step function as a LangGraph node."""
    def node(state: GraphState) -> dict:
        fn(state["ctx"])
        return {"ctx": state["ctx"]}
    return node


def route(state: GraphState) -> str:
    """Pick the next node from the context status and next_index."""
    ctx = state["ctx"]
    if ctx.status == "recover":
        return "recover"
    if ctx.status != "running" or ctx.next_index >= 4:
        return "end"
    return NODE_BY_INDEX[ctx.next_index]


def build_graph():
    """Build and compile the workflow graph (no checkpointer)."""
    g = StateGraph(GraphState)
    g.add_node("plan", _wrap(step_plan))
    g.add_node("retrieve", _wrap(step_retrieve))
    g.add_node("refund", _wrap(step_refund))
    g.add_node("email", _wrap(step_email))
    g.add_node("audit", _wrap(step_audit))
    g.add_node("recover", _wrap(handle_recovery))
    targets = {"retrieve": "retrieve", "refund": "refund", "email": "email", "audit": "audit",
               "recover": "recover", "end": END}
    g.add_edge(START, "plan")
    for name in ["plan", "retrieve", "refund", "email", "audit", "recover"]:
        g.add_conditional_edges(name, route, targets)
    return g.compile()


def run_workflow_langgraph(admin, agents, req, behaviour, hooks=None, step_delay=0.0,
                           make_agent=None) -> RunOutcome:
    """Run the workflow on LangGraph and return the RunOutcome."""
    ctx = WorkflowContext(admin=admin, agents=agents, req=req, behaviour=behaviour,
                          hooks=hooks or {}, step_delay=step_delay, make_agent=make_agent)
    out = build_graph().invoke({"ctx": ctx}, config={"recursion_limit": 50})
    return to_outcome(out["ctx"])
