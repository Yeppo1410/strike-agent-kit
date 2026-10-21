"""The real client against a fake Strike server that verifies signatures like the docs describe."""

import json
import time

import httpx
import pytest
from nacl.signing import VerifyKey

from strike_agent_kit.client import StrikeAPIError, StrikeClient
from strike_agent_kit.config import Settings
from strike_agent_kit.signing import build_message, generate_keypair


def make_client(handler, priv, pub, fee=None):
    s = Settings(mode="testnet", base_url="https://strike.test", api_public_key=pub, api_private_key=priv,
                 builder_fee_bps=fee, guardrails_path=None, journal_path=None, state_path=None,
                 hyperliquid_url="", use_hyperliquid=False)
    return StrikeClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)))


def fake_strike(pub, seen):
    server_ms = int(time.time() * 1000) + 5000  # server clock 5 s ahead of ours

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.raw_path.decode()
        if path == "/v2/time":
            return httpx.Response(200, json={"serverTime": server_ms})
        h = req.headers
        ts = int(h["X-API-Wallet-Timestamp"])
        assert ts <= server_ms // 1000, "timestamp must not be in the future"
        msg = build_message(req.method, path, ts, h["X-API-Wallet-Nonce"], req.content or None)
        VerifyKey(bytes.fromhex(pub)).verify(msg.encode(), bytes.fromhex(h["X-API-Wallet-Signature"]))
        seen.append((req.method, path, dict(h), req.content))
        if path.startswith("/v2/openOrders"):
            return httpx.Response(200, json={"orders": None, "count": 0})
        if path == "/v2/order" and req.method == "POST":
            return httpx.Response(201, json={"client_order_id": json.loads(req.content)["client_order_id"],
                                             "status": "pending"})
        return httpx.Response(400, json={"error": "X-Builder-Fee-Bps 50 exceeds your approved max of 20"})
    return handler


def test_signed_get_with_query_and_clock_sync():
    priv, pub = generate_keypair()
    seen = []
    c = make_client(fake_strike(pub, seen), priv, pub)
    assert c.get_open_orders("BTC-USD") == []
    assert seen[0][1] == "/v2/openOrders?symbol=BTC-USD"
    assert 3000 < c._clock_offset_ms < 7000


def test_signed_post_hashes_exact_body_and_sends_builder_fee():
    priv, pub = generate_keypair()
    seen = []
    c = make_client(fake_strike(pub, seen), priv, pub)
    c.place_order({"client_order_id": "agent-1", "symbol": "ADA-USD", "side": "sell", "type": "market",
                   "size": "100"}, builder_fee_bps=10)
    method, path, headers, body = seen[-1]
    assert headers["x-builder-fee-bps"] == "10"
    assert headers["content-type"] == "application/json"
    assert b" " not in body  # compact JSON, serialized once


def test_api_errors_surface_message():
    priv, pub = generate_keypair()
    c = make_client(fake_strike(pub, []), priv, pub)
    with pytest.raises(StrikeAPIError) as e:
        c.set_leverage("ADA-USD", 3)
    assert e.value.status == 400 and "approved max" in e.value.message
