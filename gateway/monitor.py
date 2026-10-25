"""Per-tool circuit breaker and the rule-based Monitor."""
import time
from collections import deque

from gateway.store import GatewayStore


class CircuitBreaker:
    def __init__(self, store: GatewayStore, cfg: dict):
        """cfg is the `circuit` section of policies.yaml."""
        self.store, self.cfg = store, cfg
        self.windows: dict[str, deque] = {}
        self.open_until: dict[str, float] = {}
        self.tripped: set[str] = set()

    def reset(self) -> None:
        """Close every circuit and forget all outcomes."""
        self.windows.clear()
        self.open_until.clear()
        self.tripped.clear()

    def is_open(self, tool: str) -> bool:
        """True while the circuit for the tool is open (after open_until it is half-open)."""
        return time.time() < self.open_until.get(tool, 0.0)

    def record(self, tool: str, success: bool, run_id: str | None = None, agent_id: str | None = None) -> None:
        """Record one outcome from the tools service and open/close the circuit as needed."""
        win = self.windows.setdefault(tool, deque(maxlen=self.cfg["window"]))
        if tool in self.tripped:
            if success:
                win.clear()
                self.tripped.discard(tool)
                self.open_until.pop(tool, None)
                self.store.add_event("tool_recovered", run_id, None, agent_id, tool)
            else:
                self.open_until[tool] = time.time() + self.cfg["open_seconds"]
            return
        win.append(success)
        failures = win.count(False)
        if not success and len(win) >= self.cfg["min_calls"] and failures / len(win) > self.cfg["error_ratio"]:
            self.tripped.add(tool)
            self.open_until[tool] = time.time() + self.cfg["open_seconds"]
            self.store.add_event("circuit_open", run_id, None, agent_id, tool, {"reason": "circuit_open"})
            self.store.add_event("anomaly_detected", run_id, None, agent_id, tool, {"kind": "tool_unhealthy"})


class Monitor:
    def __init__(self, store: GatewayStore, killswitch, recovery, policy):
        """Keep collaborators; limits come from policies.yaml."""
        self.store, self.killswitch, self.recovery = store, killswitch, recovery
        self.limits = policy.cfg["limits"]

    def _rule(self, call, agent: dict) -> str | None:
        """Return the first triggered rule name, else None."""
        lim, now = self.limits, time.time()
        since = max(now - lim["block_burst_window_seconds"], agent["created_at"])
        if self.store.count_events(agent["agent_id"], ["policy_blocked", "guard_blocked"], since) >= lim["block_burst_count"]:
            return "R1"
        steps = self.store.get_steps(call.run_id)
        if len([s for s in steps if s["agent_id"] == agent["agent_id"] and s["deviation"]]) >= lim["max_deviations_per_agent"]:
            return "R2"
        if len([s for s in steps if s["tool"] == "issue_refund" and s["side_effect"] and not s["tainted"]]) > lim["max_refunds_per_run"]:
            return "R3"
        return None

    def after_call(self, call, agent_row: dict | None) -> None:
        """Quarantine and roll back the agent if a monitor rule triggers (ON mode only)."""
        if self.store.get_config("mode") != "on" or not self.store.get_config("monitor_enabled"):
            return
        agent = self.store.get_agent(call.agent_id)
        if not agent or agent["status"] != "active":
            return
        rule = self._rule(call, agent)
        if rule is None:
            return
        self.store.add_event("anomaly_detected", call.run_id, call.step_id, agent["agent_id"], call.tool,
                             {"kind": "monitor_rule", "rule": rule})
        self.killswitch.set_status(agent["agent_id"], "quarantine", {"rule": rule})
        self.recovery.rollback(call.run_id, agent["agent_id"])
