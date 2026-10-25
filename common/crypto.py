"""Ed25519 key helpers and canonical JSON."""
import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, base64 of the raw 32-byte public key)."""
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key, base64.b64encode(raw).decode()


def canonical_json(obj: dict) -> str:
    """Deterministic JSON used for signatures, loop detection and idempotency."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign(private_key: Ed25519PrivateKey, message: str) -> str:
    """Return the base64 signature over message."""
    return base64.b64encode(private_key.sign(message.encode())).decode()


def verify(public_key_b64: str, message: str, signature_b64: str) -> bool:
    """Verify a signature; never raises."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), message.encode())
        return True
    except Exception:
        return False


def envelope_message(envelope: dict) -> str:
    """Canonical JSON of the envelope without its signature."""
    return canonical_json({k: v for k, v in envelope.items() if k != "signature"})
