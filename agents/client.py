"""Agent-side client: signs envelopes and calls the gateway."""
import json
import time
import uuid

import httpx

from common.crypto import envelope_message, generate_keypair, sign
from common.envelope import CallResult

SIDE_EFFECT_TOOLS = {"issue_refund", "send_email", "update_record"}


class AgentClient:
    def __init__(self, agent_id: str, role: str, gateway_url: str, admin_token: str,
                 http: httpx.Client | None = None):
        """Hold identity; keys are generated on register()."""
        self.agent_id = agent_id
        self.role = role
        self.admin_token = admin_token
        self.http = http or httpx.Client(base_url=gateway_url, timeout=30)
        self.private_key = None
        self.public_key = ""
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.last_request: tuple[str, dict, bytes] | None = None

    def register(self) -> None:
        """Generate a keypair and register with the gateway (admin header)."""
        self.private_key, self.public_key = generate_keypair()
        self.http.post("/identity/register", headers={"X-Admin-Token": self.admin_token},
                       json={"agent_id": self.agent_id, "role": self.role, "public_key": self.public_key})

    def ensure_token(self) -> None:
        """Fetch a token if none or it expires within 30 s."""
        if self.token and self.token_expires_at - time.time() > 30:
            return
        ts = int(time.time())
        sig = sign(self.private_key, f"token:{self.agent_id}:{ts}")
        resp = self.http.post("/identity/token", json={"agent_id": self.agent_id, "timestamp": ts,
                                                       "signature": sig})
        data = resp.json()
        self.token, self.token_expires_at = data.get("token"), data.get("expires_at", 0.0)

    def _attempt(self, tool: str, args: dict, run_id: str, step_id: str, parents: list[str],
                 idem: str | None) -> httpx.Response:
        """Sign and send one HTTP attempt with a fresh nonce and timestamp."""
        self.ensure_token()
        env = {"run_id": run_id, "step_id": step_id, "agent_id": self.agent_id, "tool": tool,
               "args": args, "parent_step_ids": parents, "idempotency_key": idem,
               "nonce": uuid.uuid4().hex, "timestamp": time.time()}
        env["signature"] = sign(self.private_key, envelope_message(env))
        body = json.dumps(env).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        self.last_request = ("/call", headers, body)
        return self.http.post("/call", headers=headers, content=body)

    def call(self, tool: str, args: dict, run_id: str, parent_step_ids: list[str] | None = None,
             idempotency_key: str | None = None) -> CallResult:
        """Call the gateway and return a CallResult."""
        step_id = uuid.uuid4().hex[:12]
        if tool in SIDE_EFFECT_TOOLS:
            idempotency_key = idempotency_key or uuid.uuid4().hex
        resp = self._attempt(tool, args, run_id, step_id, parent_step_ids or [], idempotency_key)
        return CallResult(status_code=resp.status_code, body=resp.json())
