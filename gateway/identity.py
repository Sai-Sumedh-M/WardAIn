"""Agent identity: registration, token issuing and per-call verification."""
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption, PrivateFormat,
                                                          load_pem_private_key)
from pathlib import Path

from common.crypto import envelope_message, generate_keypair, verify
from gateway.store import GatewayStore


class IdentityError(Exception):
    def __init__(self, reason: str):
        """Carry the block reason."""
        super().__init__(reason)
        self.reason = reason


class IdentityService:
    def __init__(self, store: GatewayStore, key_path: str):
        """Load (or create) the gateway signing key."""
        self.store = store
        path = Path(key_path)
        if path.exists():
            self.key: Ed25519PrivateKey = load_pem_private_key(path.read_bytes(), password=None)
        else:
            self.key, _ = generate_keypair()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        self.public_key = self.key.public_key()

    def register(self, agent_id: str, role: str, public_key: str) -> None:
        """Upsert an active agent."""
        self.store.upsert_agent(agent_id, role, public_key)
        self.store.add_event("agent_registered", agent_id=agent_id, detail={"role": role})

    def issue_token(self, agent_id: str, timestamp: int, signature: str) -> dict:
        """Issue a 300 s EdDSA JWT after verifying the signed challenge."""
        agent = self.store.get_agent(agent_id)
        if agent is None:
            raise IdentityError("unknown_agent")
        if agent["status"] == "halted":
            raise IdentityError("agent_halted")
        now = time.time()
        if abs(now - timestamp) > 30:
            raise IdentityError("clock_skew")
        if not verify(agent["public_key"], f"token:{agent_id}:{timestamp}", signature):
            raise IdentityError("bad_signature")
        jti = uuid.uuid4().hex
        claims = {"sub": agent_id, "role": agent["role"], "iat": int(now), "exp": int(now) + 300, "jti": jti}
        token = jwt.encode(claims, self.key, algorithm="EdDSA")
        self.store.add_token(jti, agent_id, claims["exp"])
        return {"token": token, "expires_at": float(claims["exp"])}

    def decode_token(self, authorization_header: str | None) -> dict:
        """Validate the bearer token and return its claims (no envelope checks)."""
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise IdentityError("missing_token")
        try:
            claims = jwt.decode(authorization_header[7:], self.public_key, algorithms=["EdDSA"])
        except jwt.ExpiredSignatureError:
            raise IdentityError("token_expired")
        except jwt.PyJWTError:
            raise IdentityError("invalid_token")
        if self.store.is_token_revoked(claims.get("jti", "")):
            raise IdentityError("token_revoked")
        return claims

    def verify_call(self, authorization_header: str | None, envelope: dict) -> dict:
        """Run the 8 identity checks in order; return the agent row."""
        claims = self.decode_token(authorization_header)
        agent = self.store.get_agent(claims.get("sub", ""))
        if agent is None:
            raise IdentityError("unknown_agent")
        if claims["sub"] != envelope.get("agent_id") or claims.get("role") != agent["role"]:
            raise IdentityError("role_mismatch")
        if not verify(agent["public_key"], envelope_message(envelope), envelope.get("signature", "")):
            raise IdentityError("bad_signature")
        now = time.time()
        if abs(now - envelope.get("timestamp", 0)) > 30:
            raise IdentityError("clock_skew")
        if not self.store.add_nonce(envelope.get("nonce", ""), now):
            raise IdentityError("replay_detected")
        return agent

    def revoke_agent(self, agent_id: str) -> None:
        """Revoke all of an agent's tokens."""
        self.store.revoke_agent_tokens(agent_id)
