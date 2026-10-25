"""Chaos engine: injects faults from the active scenario (applies in ON and OFF mode)."""
import json
import random
import time

from common.envelope import ToolCall
from gateway.store import GatewayStore

CORRUPT_TEXT = '{"result": {"customer_id": "C001", "name": '


class ChaosEngine:
    def __init__(self, store: GatewayStore):
        """Keep the store; counters and the RNG live in memory."""
        self.store = store
        self.counts: dict[tuple[str, int], int] = {}
        self.rng = random.Random(42)
        self.seed: int | None = None

    def reset(self) -> None:
        """Clear fire counters and force a re-seed on next use."""
        self.counts.clear()
        self.seed = None

    def _rules(self) -> list[dict]:
        """Chaos rules of the active scenario."""
        return (self.store.get_config("scenario") or {}).get("chaos") or []

    def _fires(self, call: ToolCall, idx: int, rule: dict) -> bool:
        """True (and counted) if the rule applies to this call now."""
        if rule.get("tool") != call.tool:
            return False
        key = (call.run_id, idx)
        if self.counts.get(key, 0) >= rule.get("times", float("inf")):
            return False
        prob = float(rule.get("probability", 1.0))
        if prob < 1.0:
            seed = (self.store.get_config("scenario") or {}).get("seed", 42)
            if seed != self.seed:
                self.rng, self.seed = random.Random(seed), seed
            if self.rng.random() >= prob:
                return False
        self.counts[key] = self.counts.get(key, 0) + 1
        self.store.add_event("fault_injected", call.run_id, call.step_id, call.agent_id, call.tool,
                             {"fault": rule["fault"], "reason": rule["fault"]})
        return True

    def pre(self, call: ToolCall) -> bool:
        """Apply latency faults (sleep); return True if an error fault fires (caller returns 502)."""
        error = False
        for idx, rule in enumerate(self._rules()):
            if rule["fault"] == "latency" and self._fires(call, idx, rule):
                time.sleep(float(rule.get("params", {}).get("ms", 0)) / 1000.0)
            elif rule["fault"] == "error" and self._fires(call, idx, rule):
                error = True
        return error

    def post(self, call: ToolCall, raw_text: str) -> tuple[str, bool]:
        """Apply response faults; return (possibly modified text, timeout_after_commit flag)."""
        timeout = False
        for idx, rule in enumerate(self._rules()):
            fault, params = rule["fault"], rule.get("params", {})
            if fault not in ("corrupt", "inject_prompt", "wrong_value", "timeout_after_commit"):
                continue
            if not self._fires(call, idx, rule):
                continue
            if fault == "timeout_after_commit":
                timeout = True
            elif fault == "corrupt":
                raw_text = CORRUPT_TEXT
            else:
                raw_text = self._modify(raw_text, fault, params)
        return raw_text, timeout

    @staticmethod
    def _modify(raw_text: str, fault: str, params: dict) -> str:
        """inject_prompt appends text to result[field]; wrong_value overwrites it."""
        try:
            data = json.loads(raw_text)
            field = params["field"]
            if fault == "inject_prompt":
                data["result"][field] = f"{data['result'].get(field, '')} {params['text']}"
            else:
                data["result"][field] = params["value"]
            return json.dumps(data)
        except (ValueError, KeyError, TypeError):
            return raw_text
