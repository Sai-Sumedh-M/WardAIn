"""Per-tool circuit breaker (the Monitor rules are added in Phase 4)."""
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
