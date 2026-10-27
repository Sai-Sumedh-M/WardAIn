"""Role permissions, data-flow rules and the high-risk approval trigger."""
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.envelope import ToolCall
from gateway.store import GatewayStore

POLICY_PATH = Path(__file__).parent / "policies.yaml"


@dataclass
class PolicyBlock:
    reason: str


def load_policies(path: Path = POLICY_PATH) -> dict:
    """Load policies.yaml."""
    return yaml.safe_load(path.read_text())


class PolicyEngine:
    def __init__(self, store: GatewayStore, path: Path = POLICY_PATH):
        """Load the policy file."""
        self.store = store
        self.cfg = load_policies(path)

    def _recipient_allowed(self, to: str, run_id: str) -> bool:
        """Internal domain, or the email of a customer retrieved in this run."""
        if to.endswith("@" + self.cfg["internal_email_domain"]):
            return True
        return any(s["tool"] == "get_customer" and s["result"].get("email") == to
                   for s in self.store.get_steps(run_id))

    def check(self, role: str, call: ToolCall, run: dict | None) -> PolicyBlock | None:
        """Return a PolicyBlock for the first violated rule, else None."""
        args = call.args
        if call.tool in self.cfg["system_only_tools"]:
            return PolicyBlock("system_only_tool")
        if call.tool not in self.cfg["roles"].get(role, []):
            return PolicyBlock("role_not_allowed")
        if call.tool == "issue_refund" and run and args.get("customer_id") != run["customer_id"]:
            return PolicyBlock("customer_out_of_scope")
        if call.tool == "send_email" and not self._recipient_allowed(str(args.get("to", "")), call.run_id):
            return PolicyBlock("recipient_not_allowed")
        return None

    def requires_approval(self, call: ToolCall) -> bool:
        """True for a high-risk call (refund above the configured amount)."""
        if call.tool != "issue_refund":
            return False
        try:
            return round(float(call.args["amount"]), 2) > self.cfg["high_risk"]["issue_refund_amount_gt"]
        except (KeyError, TypeError, ValueError):
            return False

    @property
    def approval_timeout(self) -> float:
        """Seconds before a pending approval is rejected."""
        return float(self.cfg["approval_timeout_seconds"])

    @property
    def review_timeout(self) -> float:
        """Seconds before a pending data review is released sanitized."""
        return float(self.cfg["review_timeout_seconds"])
