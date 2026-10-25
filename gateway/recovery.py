"""Plan deviation check, taint propagation, rollback and recovery status."""
import json
import time

from gateway.store import GatewayStore
from gateway.tools_client import ToolsClient, ToolsUnavailable

REAL_TOOLS = {"get_customer", "get_payments", "issue_refund", "send_email", "update_record"}


def undo_for(tool: str, result: dict) -> tuple[str, dict] | None:
    """Return (undo_tool, undo_args) for a side-effect step result, or None if not derivable."""
    try:
        if tool == "issue_refund":
            return "reverse_refund", {"payment_id": result["payment_id"]}
        if tool == "send_email":
            return "recall_email", {"email_id": result["email_id"]}
        if tool == "update_record":
            return "update_record", {"customer_id": result["customer_id"], "field": result["field"],
                                     "value": result["previous_value"]}
    except KeyError:
        return None
    return None


class Recovery:
    def __init__(self, store: GatewayStore, tools: ToolsClient):
        """Keep collaborators."""
        self.store, self.tools = store, tools

    def _resolve(self, value, run: dict, steps: list[dict]):
        """Resolve $customer_email from the latest get_customer step of the run's customer."""
        if value != "$customer_email":
            return value
        for s in reversed(steps):
            if s["tool"] == "get_customer" and s["result"].get("customer_id") == run["customer_id"]:
                return s["result"].get("email")
        return None

    @staticmethod
    def _same(a, b) -> bool:
        """Compare plan values, amounts after rounding."""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            return round(a, 2) == round(b, 2)
        return a == b

    def check_deviation(self, step: dict) -> None:
        """Match a real-tool step to the plan, or flag it as a deviation."""
        run = self.store.get_run(step["run_id"])
        if step["tool"] not in REAL_TOOLS or not run or not run["plan_json"]:
            return
        steps = self.store.get_steps(step["run_id"])
        taken = {s["plan_index"] for s in steps if s["plan_index"] is not None and not s["tainted"]
                 and s["step_id"] != step["step_id"]}
        for entry in sorted(json.loads(run["plan_json"]), key=lambda e: e["plan_index"]):
            if (entry["role"] == step["role"] and entry["tool"] == step["tool"]
                    and entry["plan_index"] not in taken
                    and all(k in step["args"] and self._same(step["args"][k], self._resolve(v, run, steps))
                            for k, v in entry["args"].items())):
                self.store.set_step_plan(step["step_id"], entry["plan_index"], 0)
                return
        self.store.set_step_plan(step["step_id"], None, 1)
        self.store.add_event("plan_deviation", step["run_id"], step["step_id"], step["agent_id"], step["tool"],
                             {"reason": "not_in_plan"})

    def tainted_steps(self, run_id: str, agent_id: str) -> list[str]:
        """Step ids tainted by the agent's misbehaviour, including their descendants."""
        steps = self.store.get_steps(run_id)
        mine = [s for s in steps if s["agent_id"] == agent_id]
        start = next((s["ts"] for s in mine if s["deviation"]), None)
        if start is None:
            start = self.store.last_event_ts(agent_id, "agent_quarantined") or time.time()
        tainted = {s["step_id"] for s in mine if s["ts"] >= start}
        changed = True
        while changed:
            changed = False
            for s in steps:
                if s["step_id"] not in tainted and tainted & set(s["parent_step_ids"]):
                    tainted.add(s["step_id"])
                    changed = True
        return [s["step_id"] for s in steps if s["step_id"] in tainted]

    def rollback(self, run_id: str, agent_id: str) -> None:
        """Taint the agent's bad steps and apply their compensations in reverse order."""
        tainted = self.tainted_steps(run_id, agent_id)
        self.store.mark_tainted(tainted)
        self.store.add_event("rollback_started", run_id, None, agent_id,
                             detail={"agent_id": agent_id, "tainted_step_ids": tainted})
        done = 0
        for comp in self.store.pending_compensations(run_id, tainted):
            try:
                status, _ = self.tools.call(comp["undo_tool"], comp["undo_args"])
            except ToolsUnavailable:
                continue
            if status != 200:
                continue
            self.store.mark_compensation_applied(comp["id"])
            self.store.add_event("compensation_applied", run_id, comp["step_id"], agent_id, comp["tool"],
                                 {"undo_tool": comp["undo_tool"], "step_id": comp["step_id"]})
            done += 1
        self.store.set_run_status(run_id, "rolled_back")
        self.store.add_event("rollback_completed", run_id, None, agent_id,
                             detail={"compensated": done, "next_plan_index": self.status(run_id)["next_plan_index"]})

    def status(self, run_id: str) -> dict:
        """Recovery status: completed plan steps, next plan index and quarantined agents."""
        run = self.store.get_run(run_id)
        steps = self.store.get_steps(run_id)
        completed = sorted(({"plan_index": s["plan_index"], "step_id": s["step_id"], "tool": s["tool"],
                             "result": s["result"]} for s in steps
                            if s["plan_index"] is not None and not s["tainted"]),
                           key=lambda c: c["plan_index"])
        done = {c["plan_index"] for c in completed}
        nxt = next((i for i in range(4) if i not in done), 4)
        quarantined = sorted({s["agent_id"] for s in steps
                              if (self.store.get_agent(s["agent_id"]) or {}).get("status") == "quarantined"})
        return {"run_id": run_id, "status": run["status"] if run else None, "completed": completed,
                "next_plan_index": nxt, "quarantined_agents": quarantined}
