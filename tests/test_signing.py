from nacl.signing import VerifyKey

from strike_agent_kit.signing import (
    EMPTY_BODY_SHA256, body_hash, build_message, generate_keypair, load_signing_key, sign_headers,
)


def test_empty_body_hash_matches_docs():
    assert body_hash(None) == EMPTY_BODY_SHA256
    assert body_hash(b"") == EMPTY_BODY_SHA256


def test_message_format():
    msg = build_message("get", "/v2/openOrders?symbol=BTC-USD", 1700000000, "nonce-1234567890ab", None)
    assert msg == f"GET:/v2/openOrders?symbol=BTC-USD:1700000000:nonce-1234567890ab:{EMPTY_BODY_SHA256}"


def test_headers_verify_with_public_key():
    priv, pub = generate_keypair()
    key = load_signing_key(priv)
    body = b'{"symbol":"ADA-USD","side":"buy"}'
    h = sign_headers(key, pub, "POST", "/v2/order", body, 1700000000, "n" * 32)
    assert h["X-API-Wallet-Public-Key"] == pub and len(pub) == 64
    assert len(h["X-API-Wallet-Signature"]) == 128
    assert h["X-API-Wallet-Timestamp"] == "1700000000"
    msg = build_message("POST", "/v2/order", 1700000000, "n" * 32, body)
    VerifyKey(bytes.fromhex(pub)).verify(msg.encode(), bytes.fromhex(h["X-API-Wallet-Signature"]))


def test_accepts_128_hex_private_key():
    priv, pub = generate_keypair()
    key = load_signing_key(priv + pub)
    assert key.verify_key.encode().hex() == pub


def test_fresh_nonce_per_request():
    priv, pub = generate_keypair()
    key = load_signing_key(priv)
    a = sign_headers(key, pub, "GET", "/v2/account", None, 1)
    b = sign_headers(key, pub, "GET", "/v2/account", None, 1)
    assert a["X-API-Wallet-Nonce"] != b["X-API-Wallet-Nonce"]
    assert 16 <= len(a["X-API-Wallet-Nonce"]) <= 128
