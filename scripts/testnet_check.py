"""First thing to run with real keys: checks clock sync, public data, signing and account access.

    python scripts/testnet_check.py            # testnet or mainnet, per .env
    python scripts/testnet_check.py --public   # no keys needed: time, markets and funding on testnet + mainnet

Places no orders.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strike_agent_kit.client import StrikeAPIError, StrikeClient  # noqa: E402
from strike_agent_kit.config import BASE_URLS, HYPERLIQUID_INFO_URL, Settings  # noqa: E402
from strike_agent_kit.hyperliquid import HyperliquidFunding  # noqa: E402


def step(name, fn):
    try:
        out = fn()
        print(f"[ok]   {name}: {out}")
        return True
    except StrikeAPIError as e:
        print(f"[FAIL] {name}: HTTP {e.status} {e.message}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    return False


def _markets(c):
    rows, markets = [], c.get_markets()
    for k, v in markets.items():
        rows.append(f"\n         {k} {v.get('status')} mark={v.get('mark_price')} funding={v.get('funding_rate')} "
                    f"min_notional={v.get('order_min_notional')} min_size={v.get('order_market_min_size')}")
    first = next(iter(markets.values()), {})
    rows.append(f"\n         field names: {sorted(first)}")
    return "".join(rows)


def public_only() -> int:
    """Unsigned endpoints only, so this runs before any wallet or deposit exists."""
    ok = True
    for net, url in BASE_URLS.items():
        print(f"== {net}: {url}")
        c = StrikeClient(Settings(mode=net, base_url=url, api_public_key=None, api_private_key=None,
                                  builder_fee_bps=None, guardrails_path=Path("guardrails.json"),
                                  journal_path=Path("/dev/null"), state_path=Path("/dev/null"),
                                  hyperliquid_url=HYPERLIQUID_INFO_URL, use_hyperliquid=True))
        ok &= step("clock sync", lambda: (c.sync_clock(), f"offset {c._clock_offset_ms} ms")[1])
        ok &= step("markets (public)", lambda: _markets(c))
        print()
    step("hyperliquid funding (public)", lambda: {k: str(v["funding_hourly"]) for k, v in list(
        HyperliquidFunding(HYPERLIQUID_INFO_URL).get_funding().items())[:5]})
    print("\nPublic checks passed." if ok else "\nSome public checks failed; paste the output to Claude.")
    return 0 if ok else 1


def main() -> int:
    if "--public" in sys.argv:
        return public_only()
    s = Settings.from_env()
    if s.is_mock:
        print("STRIKE_MODE is mock; set STRIKE_MODE=testnet (or mainnet) in .env first.")
        return 1
    c = StrikeClient(s)
    print(f"Base URL: {s.base_url}\nPublic key: {s.api_public_key}\n")

    ok = step("clock sync", lambda: (c.sync_clock(), f"offset {c._clock_offset_ms} ms")[1])
    ok &= step("markets (public)", lambda: _markets(c))
    ok &= step("account (signed)", lambda: {k: c.get_account().get(k) for k in
                                            ("account_id", "wallet_balance", "available_balance", "default_leverage")})
    ok &= step("positions (signed)", lambda: c.get_positions())
    ok &= step("open orders (signed, with query string)", lambda: c.get_open_orders("BTC-USD"))
    step("hyperliquid funding (public)", lambda: {k: str(v["funding_hourly"]) for k, v in list(
        HyperliquidFunding(s.hyperliquid_url).get_funding().items())[:5]})
    print("\nAll Strike checks passed." if ok else "\nSome checks failed; paste the output to Claude.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
