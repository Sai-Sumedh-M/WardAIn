"""Kill-switch: per-agent pause/quarantine/halt and global halt."""
from gateway.identity import IdentityService
from gateway.policy import PolicyEngine
from gateway.store import GatewayStore

ACTIONS = {"pause": ("paused", "agent_paused"), "resume": ("active", "agent_resumed"),
           "quarantine": ("quarantined", "agent_quarantined"), "halt": ("halted", "agent_halted")}


class KillSwitch:
    def __init__(self, store: GatewayStore, identity: IdentityService, policy: PolicyEngine):
        """Keep collaborators."""
        self.store, self.identity, self.policy = store, identity, policy

    def check(self, agent_row: dict | None, tool: str) -> str | None:
        """Return the block reason if the agent may not act, else None."""
        if self.store.get_config("global_halt"):
            return "global_halt"
        status = agent_row["status"] if agent_row else "active"
        if status == "paused":
            return "agent_paused"
        if status == "halted":
            return "agent_halted"
        if status == "quarantined" and tool not in self.policy.cfg["quarantine_allowed_tools"]:
            return "agent_quarantined"
        return None

    def set_status(self, agent_id: str, action: str, detail: dict | None = None) -> None:
        """Apply pause | resume | quarantine | halt to an agent."""
        status, event = ACTIONS[action]
        self.store.set_agent_status(agent_id, status)
        if action == "halt":
            self.identity.revoke_agent(agent_id)
        self.store.add_event(event, agent_id=agent_id, detail=detail or {})

    def halt_all(self) -> None:
        """Block every agent."""
        self.store.set_config("global_halt", True)
        self.store.add_event("global_halt")

    def resume_all(self) -> None:
        """Clear the global halt."""
        self.store.set_config("global_halt", False)
        self.store.add_event("global_resume")
