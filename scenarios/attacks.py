"""Attack helpers: a rogue client and a replay helper."""
import json
import time
import uuid

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.client import AgentClient
from common.crypto import envelope_message, generate_keypair, sign


class RogueClient:
    def __init__(self, gateway_url: str, http: httpx.Client | None = None):
        """Raw HTTP client that bypasses AgentClient."""
        self.http = http or httpx.Client(base_url=gateway_url, timeout=30)

    def mint_jwt(self, agent_id: str, role: str, key: Ed25519PrivateKey) -> str:
        """Forge a JWT for an agent, signed with the given (rogue) key."""
        now = int(time.time())
        claims = {"sub": agent_id, "role": role, "iat": now, "exp": now + 300, "jti": uuid.uuid4().hex}
        return jwt.encode(claims, key, algorithm="EdDSA")

    def send(self, agent_id: str, run_id: str, tool: str, args: dict, sign_key: Ed25519PrivateKey,
             token: str | None, timestamp: float | None = None) -> int:
        """POST a signed envelope as agent_id; return the status code."""
        env = {"run_id": run_id, "step_id": uuid.uuid4().hex[:12], "agent_id": agent_id, "tool": tool,
               "args": args, "parent_step_ids": [], "idempotency_key": uuid.uuid4().hex,
               "nonce": uuid.uuid4().hex, "timestamp": timestamp or time.time()}
        env["signature"] = sign(sign_key, envelope_message(env))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.http.post("/call", headers=headers, content=json.dumps(env).encode()).status_code

    def impersonate(self, run_id: str, victim: AgentClient) -> list[int]:
        """3 attempts to issue a refund as the victim; return the status codes."""
        rogue, _ = generate_keypair()
        args = {"customer_id": "C001", "amount": 500.0}
        victim.ensure_token()
        attempts = [(None), (self.mint_jwt(victim.agent_id, victim.role, rogue)), victim.token]
        return [self.send(victim.agent_id, run_id, "issue_refund", args, rogue, tok) for tok in attempts]


def replay(victim: AgentClient, count: int, http: httpx.Client | None = None) -> list[int]:
    """Resend the victim's last HTTP request (same headers and body bytes) count times."""
    url, headers, body = victim.last_request
    client = http or victim.http
    return [client.post(url, headers=headers, content=body).status_code for _ in range(count)]
