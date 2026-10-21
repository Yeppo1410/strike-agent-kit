"""In-memory stand-in for Strike, so the whole kit runs without keys or network.

It mirrors the response shapes of the real API closely enough that the agent
service runs the same code path in mock and testnet mode. Prices are made-up
demo values, not live data.
"""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal as D
from typing import Any

TAKER_FEE = D("0.0005")


def _market(symbol: str, base: str, mark: str, funding: str, tick: str, step: str,
            min_price: str, max_lev: int) -> dict[str, Any]:
    return {
        "symbol": symbol, "name": f"{base} / USD", "base_asset": base, "status": "trading",
        "base_prec": 8, "quote_prec": 8, "default_leverage": 10,
        "margin_tiers": [{"max_notional": "300000", "max_leverage": max_lev,
                          "maintenance_margin_rate": "0.004", "maintenance_amount": "0"}],
        "reduce_only": False,
        "order_tick_price": tick, "order_min_price": min_price, "order_max_price": "0",
        "order_limit_price_bound": "0.05", "order_market_price_bound": "0.05",
        "order_limit_step_size": step, "order_limit_min_size": step, "order_limit_max_size": "1000000",
        "order_market_step_size": step, "order_market_min_size": step, "order_market_max_size": "1000000",
        "order_min_notional": "10",
        "mark_price": mark, "index_price": mark, "last_price": mark,
        "bid1_price": mark, "ask1_price": mark,
        "funding_rate": funding, "next_funding_time": 0, "impact_notional": "2500",
    }


# Demo values. Strike funding is hourly; these are chosen so the funding board has
# something to show (ADA and SOL differ noticeably from the mock Hyperliquid rates).
DEFAULT_MARKETS = [
    _market("BTC-USD", "BTC", "95000", "0.0000125", "0.1", "0.00001", "10", 50),
    _market("ETH-USD", "ETH", "3600", "0.0000140", "0.01", "0.0001", "1", 50),
    _market("SOL-USD", "SOL", "180", "0.0000600", "0.001", "0.01", "0.1", 20),
    _market("ADA-USD", "ADA", "0.72", "0.0001100", "0.0001", "1", "0.01", 20),
]
MOCK_HYPERLIQUID_FUNDING = {  # hourly rates
    "BTC": D("0.0000125"), "ETH": D("0.0000100"), "SOL": D("0.0000125"), "ADA": D("0.0000080"),
}


class MockStrike:
    def __init__(self, wallet_balance: str = "10000"):
        self.markets = {m["symbol"]: dict(m) for m in DEFAULT_MARKETS}
        self.wallet = D(wallet_balance)
        self.positions: dict[str, dict[str, Any]] = {}  # symbol -> {size, entry, leverage}
        self.leverage: dict[str, int] = {}
        self.orders: dict[str, dict[str, Any]] = {}  # client_order_id -> order
        self._ids = itertools.count(1000)
        self.account_id = "mock-0000-0000-0000-000000000001"
        self.builder_fees_paid = D(0)
        self.book_gap: dict[str, D] = {}  # symbol -> fraction the book sits away from mark (thin-book tests)

    # ---- test helpers ----------------------------------------------------
    def move_price(self, symbol: str, pct: float) -> None:
        m = self.markets[symbol]
        new = D(m["mark_price"]) * (1 + D(str(pct)) / 100)
        for k in ("mark_price", "index_price", "last_price", "bid1_price", "ask1_price"):
            m[k] = str(new)

    def _mark(self, symbol: str) -> D:
        return D(self.markets[symbol]["mark_price"])

    def _upnl(self) -> D:
        return sum(((self._mark(s) - p["entry"]) * p["size"] for s, p in self.positions.items()), D(0))

    def _initial_margin(self) -> D:
        return sum((abs(p["size"]) * self._mark(s) / p["leverage"] for s, p in self.positions.items()), D(0))

    # ---- reads -----------------------------------------------------------
    def get_markets(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.markets.items()}

    def get_account(self) -> dict[str, Any]:
        upnl = self._upnl()
        return {
            "account_id": self.account_id, "blockchain": "mock",
            "wallet_balance": str(self.wallet), "unrealized_pnl": str(upnl),
            "margin_balance": str(self.wallet + upnl),
            "available_balance": str(self.wallet + upnl - self._initial_margin()),
            "position_initial_margin": str(self._initial_margin()),
            "default_margin_mode": "cross", "default_leverage": 10,
            "symbol_settings": {s: {"margin_mode": "cross", "leverage": lev} for s, lev in self.leverage.items()},
        }

    def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for s, p in self.positions.items():
            out.append({
                "symbol": s, "size": str(p["size"]), "entry_price": str(p["entry"]),
                "leverage": p["leverage"], "margin_mode": "cross",
                "upnl": str((self._mark(s) - p["entry"]) * p["size"]),
                "liquidation_price": "0", "accumulated_funding_fees": "0",
            })
        return out

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self.orders.values()
                if o["Status"] == "open" and (symbol is None or o["Symbol"] == symbol)]

    def get_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        o = self.orders.get(client_order_id)
        return o if o and o["Symbol"] == symbol else None

    # ---- writes ----------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        max_lev = self.markets[symbol]["margin_tiers"][0]["max_leverage"]
        if leverage > max_lev:
            raise ValueError(f"leverage exceeds maximum allowed ({max_lev})")
        self.leverage[symbol] = leverage
        if symbol in self.positions:
            self.positions[symbol]["leverage"] = leverage
        return {"symbol": symbol, "leverage": leverage}

    def cancel_all(self, symbol: str | None = None) -> dict[str, Any]:
        for o in self.orders.values():
            if o["Status"] == "open" and (symbol is None or o["Symbol"] == symbol):
                o["Status"], o["CloseReason"] = "canceled", "canceled_by_user"
        return {"account_id": self.account_id, "canceled_count": -1}

    def place_order(self, order: dict[str, Any], builder_fee_bps: int | None = None) -> dict[str, Any]:
        symbol, side, otype = order["symbol"], order["side"], order["type"]
        coid = order.get("client_order_id") or str(uuid.uuid4())
        size = D(str(order["size"]))
        record = {
            "ID": next(self._ids), "ClientOrderID": coid, "Symbol": symbol, "Side": side, "Type": otype,
            "Size": str(size), "Filled": "0", "Price": str(order.get("price", "0")), "Status": "pending",
            "CloseReason": "", "ReduceOnly": bool(order.get("reduce_only")),
            "Builder": {"Code": "mock-builder", "FeeBps": builder_fee_bps} if builder_fee_bps else None,
        }
        self.orders[coid] = record
        mark = self._mark(symbol)

        marketable = otype == "market" or (
            otype == "limit" and ((side == "buy" and D(str(order["price"])) >= mark)
                                  or (side == "sell" and D(str(order["price"])) <= mark)))
        if not marketable:
            record["Status"] = "open"
            return {"client_order_id": coid, "symbol": symbol, "status": "pending"}

        gap = self.book_gap.get(symbol, D(0))
        if otype == "market" and gap > D(str(order.get("slippage") or "0.005")):
            record["Status"], record["CloseReason"] = "canceled", "market_order_exceeded_slippage"
            return {"client_order_id": coid, "symbol": symbol, "status": "pending"}

        signed = size if side == "buy" else -size
        pos = self.positions.get(symbol)
        current = pos["size"] if pos else D(0)
        if record["ReduceOnly"]:
            if current == 0 or (current > 0) == (signed > 0):
                record["Status"], record["CloseReason"] = "rejected", "no_open_position"
                return {"client_order_id": coid, "symbol": symbol, "status": "pending"}
            # clamp to the position size, keeping the closing direction
            signed = -min(abs(signed), abs(current)) if current > 0 else min(abs(signed), abs(current))

        new_size = current + signed
        lev = self.leverage.get(symbol, self.markets[symbol]["default_leverage"])
        if abs(new_size) > abs(current):  # margin check on risk-increasing fills
            extra_im = (abs(new_size) - abs(current)) * mark / lev
            account = self.get_account()
            if extra_im > D(account["available_balance"]):
                record["Status"], record["CloseReason"] = "rejected", "insufficient_balance"
                return {"client_order_id": coid, "symbol": symbol, "status": "pending"}

        notional = abs(signed) * mark
        fee = notional * TAKER_FEE
        bfee = notional * D(builder_fee_bps or 0) / D(10000)
        self.wallet -= fee + bfee
        self.builder_fees_paid += bfee

        if pos is None or current == 0:
            self.positions[symbol] = {"size": new_size, "entry": mark, "leverage": lev}
        elif (current > 0) == (signed > 0):  # adding
            pos["entry"] = (pos["entry"] * abs(current) + mark * abs(signed)) / abs(new_size)
            pos["size"] = new_size
        else:  # reducing / flipping
            closed = min(abs(signed), abs(current))
            self.wallet += (mark - pos["entry"]) * closed * (1 if current > 0 else -1)
            if new_size == 0:
                del self.positions[symbol]
            elif (new_size > 0) != (current > 0):
                self.positions[symbol] = {"size": new_size, "entry": mark, "leverage": lev}
            else:
                pos["size"] = new_size

        record["Status"], record["Filled"], record["AvgPrice"] = "filled", str(abs(signed)), str(mark)
        return {"client_order_id": coid, "symbol": symbol, "status": "pending"}


class MockHyperliquid:
    def get_funding(self) -> dict[str, dict[str, D]]:
        return {coin: {"funding_hourly": r, "mark": D(0)} for coin, r in MOCK_HYPERLIQUID_FUNDING.items()}
