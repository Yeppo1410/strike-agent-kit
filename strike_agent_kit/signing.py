"""Strike API wallet request signing (Ed25519).

Message format, per Strike's docs:

    {METHOD}:{PATH_WITH_QUERY}:{TIMESTAMP_SECONDS}:{NONCE}:{SHA256_HEX(BODY)}

The body hash is SHA-256 of the exact bytes sent; with no body it is SHA-256 of
empty input, never an empty string.
"""

from __future__ import annotations

import hashlib
import uuid

from nacl.signing import SigningKey

EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def body_hash(body: bytes | None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def build_message(method: str, path_with_query: str, timestamp: int, nonce: str, body: bytes | None) -> str:
    return f"{method.upper()}:{path_with_query}:{timestamp}:{nonce}:{body_hash(body)}"


def load_signing_key(private_key_hex: str) -> SigningKey:
    """Accepts the 32-byte seed (64 hex chars) or a 64-byte seed+public key (128 hex chars)."""
    raw = bytes.fromhex(private_key_hex.strip().removeprefix("0x"))
    if len(raw) == 64:
        raw = raw[:32]
    if len(raw) != 32:
        raise ValueError("Ed25519 private key must be 32 bytes (64 hex chars)")
    return SigningKey(raw)


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_hex, public_key_hex)."""
    key = SigningKey.generate()
    return key.encode().hex(), key.verify_key.encode().hex()


def sign_headers(
    signing_key: SigningKey,
    public_key_hex: str,
    method: str,
    path_with_query: str,
    body: bytes | None,
    timestamp: int,
    nonce: str | None = None,
) -> dict[str, str]:
    nonce = nonce or str(uuid.uuid4())
    message = build_message(method, path_with_query, timestamp, nonce, body)
    signature = signing_key.sign(message.encode("utf-8")).signature.hex()
    return {
        "X-API-Wallet-Public-Key": public_key_hex.lower(),
        "X-API-Wallet-Signature": signature,
        "X-API-Wallet-Timestamp": str(timestamp),
        "X-API-Wallet-Nonce": nonce,
    }


if __name__ == "__main__":  # python -m strike_agent_kit.signing  -> new keypair
    priv, pub = generate_keypair()
    print(f"STRIKE_API_PRIVATE_KEY={priv}")
    print(f"STRIKE_API_PUBLIC_KEY={pub}")
