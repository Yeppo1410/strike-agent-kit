"""The agent-facing logic. The MCP server is a thin wrapper around this class.

Every order goes through the same path:
    normalize -> round to market rules -> guardrails -> (preview | confirm) -> submit -> confirm fill -> log
"""

from __future__ import annotations

import threading
import time
import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal as D
from typing import Any

from .client import StrikeAPIError, StrikeBackend
from .config import Settings
from .guardrails import AccountState, OrderIntent, PolicyStore, evaluate
from .journal import Journal

STRIKE_TAKER_FEE = D("0.0005")
HYPERLIQUID_TAKER_FEE = D("0.00045")
HOURS_PER_YEAR = D(24 * 365)


class OrderRejected(Exception):
    pass


def _slippage_rejected(res: dict[str, Any]) -> bool:
    return res.get("status") == "rejected" and "slippage" in str(res.get("close_reason") or "")


def _f(x: D, places: int = 6) -> float:
    return float(round(x, places))


def round_to_grid(value: D, origin: D, step: D, rounding=ROUND_DOWN) -> D:
    """Snap to origin + k*step (Strike's size and price grids)."""
    if step <= 0:
        return value
    k = ((value - origin) / step).to_integral_value(rounding=rounding)
    if k < 0:
        return D(0)
    # normalize() strips trailing zeros; format 'f' avoids exponents like 3E+2 in the API payload
    return D(format((origin + k * step).normalize(), "f"))


class AgentService:
    def __init__(self, settings: Settings, backend: StrikeBackend, funding_feed: Any | None,
                 policy_store: PolicyStore, journal: Journal, poll_timeout_s: float = 5.0):
        self.settings = settings
        self.backend = backend
        self.funding_feed = funding_feed
        self.policies = policy_store
        self.journal = journal
        self.poll_timeout_s = poll_timeout_s
        # The MCP server and the dashboard run in different threads of one process.
        self.lock = threading.RLock()

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        s = symbol.strip().upper()
        return s if "-" in s else f"{s}-USD"

    def _market(self, symbol: str) -> dict[str, Any]:
        markets = self.backend.get_markets()
        if symbol not in markets:
            raise OrderRejected(f"Unknown market {symbol}. Available: {', '.join(sorted(markets))}")
        m = markets[symbol]
        if m.get("status") != "trading":
            raise OrderRejected(f"{symbol} is not open for trading (status {m.get('status')}).")
        return m

    def _state(self) -> tuple[AccountState, dict[str, Any], D]:
        account = self.backend.get_account()
        positions = self.backend.get_positions()
        markets = self.backend.get_markets()
        equity = D(account["wallet_balance"]) + D(account.get("unrealized_pnl") or "0")
        sizes = {p["symbol"]: D(p["size"]) for p in positions}
        state = AccountState(
            positions=sizes,
            marks={s: D(m["mark_price"]) for s, m in markets.items()},
            day_pnl_usd=self.journal.day_pnl(equity, D(account["wallet_balance"]), sizes),
            current_leverage={p["symbol"]: int(p["leverage"]) for p in positions if p.get("leverage")},
        )
        return state, account, equity

    @staticmethod
    def _current_leverage(symbol: str, market: dict[str, Any], account: dict[str, Any], state: AccountState) -> int:
        if symbol in state.current_leverage:
            return state.current_leverage[symbol]
        sym_settings = (account.get("symbol_settings") or {}).get(symbol) or {}
        for v in (sym_settings.get("leverage"), account.get("default_leverage"), market.get("default_leverage")):
            if v:
                return int(v)
        return 1

    # ---- reads -----------------------------------------------------------
    def get_markets(self) -> list[dict[str, Any]]:
        out = []
        for s, m in sorted(self.backend.get_markets().items()):
            out.append({
                "symbol": s, "status": m.get("status"), "mark_price": m.get("mark_price"),
                "funding_rate_hourly": m.get("funding_rate"),
                "max_leverage": (m.get("margin_tiers") or [{}])[0].get("max_leverage"),
                "min_order_size": m.get("order_market_min_size"), "size_step": m.get("order_market_step_size"),
                "min_notional_usd": m.get("order_min_notional"),
            })
        return out

    def get_account(self) -> dict[str, Any]:
        state, account, equity = self._state()
        policy = self.policies.load()
        positions = []
        for p in self.backend.get_positions():
            size = D(p["size"])
            mark = state.marks.get(p["symbol"], D(0))
            positions.append({
                "symbol": p["symbol"], "side": "long" if size > 0 else "short", "size": str(abs(size)),
                "entry_price": p["entry_price"], "mark_price": str(mark), "notional_usd": _f(abs(size) * mark, 2),
                "unrealized_pnl_usd": _f(D(p.get("upnl") or "0"), 2), "leverage": p.get("leverage"),
                "liquidation_price": p.get("liquidation_price"),
            })
        return {
            "mode": self.settings.mode,
            "equity_usd": _f(equity, 2),
            "available_usd": _f(D(account.get("available_balance") or "0"), 2),
            "unrealized_pnl_usd": _f(D(account.get("unrealized_pnl") or "0"), 2),
            "day_pnl_usd": _f(state.day_pnl_usd, 2),
            "day_external_flows_usd": _f(self.journal.day_flows(), 2),
            "daily_loss_limit_usd": _f(policy.daily_loss_limit_usd, 2),
            "kill_switch": policy.kill_switch,
            "positions": positions,
            "open_orders": len(self.backend.get_open_orders()),
        }

    def what_if(self, moves: dict[str, float] | None = None, all_markets_pct: float | None = None,
                spot: dict[str, float] | None = None, collateral_asset: str | None = None) -> dict[str, Any]:
        """Read-only price scenario: re-price positions, spot holdings and (optionally) collateral.

        moves: % change per asset, e.g. {"ADA": -20}. all_markets_pct: % change for every other market.
        spot: holdings outside Strike, e.g. {"ADA": 5000}, to see how well they are hedged.
        collateral_asset: set when the Strike margin is held in a volatile asset (e.g. "ADA"), so the
        wallet balance moves with that asset's price. Approximation: treats the whole wallet as that asset.
        """
        moves = {self.normalize_symbol(k): D(str(v)) for k, v in (moves or {}).items()}
        default_move = D(str(all_markets_pct)) if all_markets_pct is not None else D(0)
        markets = self.backend.get_markets()
        account = self.backend.get_account()
        positions = self.backend.get_positions()
        policy = self.policies.load()
        state, _, equity = self._state()

        def move_for(symbol: str) -> D:
            return moves.get(symbol, default_move)

        def shocked(symbol: str) -> tuple[D, D]:
            mark = D(markets[symbol]["mark_price"]) if symbol in markets else D(0)
            return mark, mark * (1 + move_for(symbol) / 100)

        def maint(symbol: str, notional: D) -> D:
            tiers = (markets.get(symbol) or {}).get("margin_tiers") or [{}]
            tier = next((t for t in tiers if D(str(t.get("max_notional") or "0")) >= notional), tiers[-1])
            return notional * D(str(tier.get("maintenance_margin_rate") or "0")) + D(str(tier.get("maintenance_amount") or "0"))

        rows, perp_pnl, maint_now, maint_after = [], D(0), D(0), D(0)
        for p in positions:
            sym, size = p["symbol"], D(p["size"])
            if size == 0:
                continue
            mark, new = shocked(sym)
            pnl = (new - mark) * size
            perp_pnl += pnl
            maint_now += maint(sym, abs(size) * mark)
            maint_after += maint(sym, abs(size) * new)
            rows.append({"symbol": sym, "side": "long" if size > 0 else "short", "size": str(abs(size)),
                         "move_pct": float(move_for(sym)), "mark_now": _f(mark, 6), "mark_after": _f(new, 6),
                         "pnl_usd": _f(pnl, 2)})

        spot_rows, spot_pnl = [], D(0)
        for asset, amount in (spot or {}).items():
            sym = self.normalize_symbol(asset)
            if sym not in markets:
                spot_rows.append({"asset": asset, "error": f"No Strike market {sym} to price it."})
                continue
            mark, new = shocked(sym)
            amt = D(str(amount))
            pnl = (new - mark) * amt
            spot_pnl += pnl
            perp = next((D(p["size"]) for p in positions if p["symbol"] == sym), D(0))
            hedged = min(max(-perp / amt, D(0)), D(1)) if amt > 0 else D(0)
            spot_rows.append({"asset": asset.upper(), "amount": str(amt), "value_now_usd": _f(amt * mark, 2),
                              "value_after_usd": _f(amt * new, 2), "pnl_usd": _f(pnl, 2),
                              "hedged_pct": _f(hedged * 100, 1)})

        collateral_pnl = D(0)
        coll = None
        if collateral_asset:
            sym = self.normalize_symbol(collateral_asset)
            if sym in markets:
                wallet = D(account["wallet_balance"])
                collateral_pnl = wallet * move_for(sym) / 100
                coll = {"asset": collateral_asset.upper(), "wallet_usd": _f(wallet, 2),
                        "move_pct": float(move_for(sym)), "pnl_usd": _f(collateral_pnl, 2),
                        "note": "Approximation: treats the whole wallet balance as held in this asset."}

        equity_after = equity + perp_pnl + collateral_pnl
        day_after = state.day_pnl_usd + perp_pnl  # collateral moves are excluded from day PnL by design
        liquidated = bool(rows) and equity_after <= maint_after
        warnings = []
        if liquidated:
            warnings.append("Account would be at or below maintenance margin: liquidation risk.")
        if policy.daily_loss_limit_usd > 0 and day_after <= -policy.daily_loss_limit_usd:
            warnings.append(f"Daily loss limit ({policy.daily_loss_limit_usd} USD) would trip: "
                            "new risk would be blocked, closes still allowed.")
        unhedged = [r for r in spot_rows if "hedged_pct" in r and r["hedged_pct"] < 50 and r["pnl_usd"] < 0]
        for r in unhedged:
            warnings.append(f"{r['asset']} spot is {r['hedged_pct']}% hedged; hedge_exposure can cover it.")
        if coll and collateral_pnl < 0 and any(r.get("asset") == coll["asset"] for r in spot_rows):
            warnings.append(f"Margin is held in {coll['asset']}, which adds to the same exposure as the spot holding.")

        return {
            "scenario": {"moves_pct": {k: float(v) for k, v in moves.items()},
                         "all_other_markets_pct": float(default_move)},
            "positions": rows, "spot": spot_rows, "collateral": coll,
            "totals": {"perp_pnl_usd": _f(perp_pnl, 2), "spot_pnl_usd": _f(spot_pnl, 2),
                       "collateral_pnl_usd": _f(collateral_pnl, 2),
                       "net_pnl_usd": _f(perp_pnl + spot_pnl + collateral_pnl, 2)},
            "account": {"equity_now_usd": _f(equity, 2), "equity_after_usd": _f(equity_after, 2),
                        "maintenance_margin_now_usd": _f(maint_now, 2),
                        "maintenance_margin_after_usd": _f(maint_after, 2),
                        "liquidation_buffer_after_usd": _f(equity_after - maint_after, 2),
                        "liquidation_risk": liquidated,
                        "day_pnl_after_usd": _f(day_after, 2),
                        "daily_loss_limit_usd": _f(policy.daily_loss_limit_usd, 2)},
            "warnings": warnings,
            "note": "Read-only estimate at a single instant: ignores fees, funding, slippage and liquidation "
                    "mechanics beyond the maintenance-margin check.",
        }

    def get_guardrails(self) -> dict[str, Any]:
        return {"policy": self.policies.load().to_dict(), "policy_file": str(self.settings.guardrails_path),
                "note": "Edit the policy file to change limits. The agent can engage the kill switch, "
                        "but only a human can release it."}

    def get_funding_board(self, symbols: list[str] | None = None) -> dict[str, Any]:
        markets = self.backend.get_markets()
        hl: dict[str, dict[str, D]] = {}
        hl_error = None
        if self.funding_feed is not None:
            try:
                hl = self.funding_feed.get_funding()
            except Exception as e:  # keep the Strike side useful even if Hyperliquid is down
                hl_error = str(e)
        wanted = {self.normalize_symbol(s) for s in symbols} if symbols else None
        round_trip_fees = 2 * (STRIKE_TAKER_FEE + HYPERLIQUID_TAKER_FEE)

        rows = []
        for sym, m in markets.items():
            if wanted and sym not in wanted:
                continue
            if not wanted and m.get("status") != "trading":
                continue  # e.g. pre_trading listings with mark 0; still shown when asked for by name
            strike_rate = D(m.get("funding_rate") or "0")
            row: dict[str, Any] = {
                "symbol": sym,
                "strike_hourly": _f(strike_rate, 8),
                "strike_apr_pct": _f(strike_rate * HOURS_PER_YEAR * 100, 2),
            }
            base = m.get("base_asset") or sym.split("-")[0]
            if base in hl:
                hl_rate = hl[base]["funding_hourly"]
                spread = strike_rate - hl_rate
                row.update({
                    "hyperliquid_hourly": _f(hl_rate, 8),
                    "hyperliquid_apr_pct": _f(hl_rate * HOURS_PER_YEAR * 100, 2),
                    "spread_hourly": _f(spread, 8),
                    "spread_apr_pct": _f(abs(spread) * HOURS_PER_YEAR * 100, 2),
                    "trade": ("short Strike / long Hyperliquid" if spread > 0
                              else "long Strike / short Hyperliquid" if spread < 0 else "no edge"),
                    "breakeven_hours": _f(round_trip_fees / abs(spread), 1) if spread != 0 else None,
                })
            rows.append(row)
        rows.sort(key=lambda r: abs(r.get("spread_hourly") or 0), reverse=True)
        return {
            "rows": rows,
            "notes": "Rates are hourly funding (positive = longs pay shorts). APR is the hourly rate x 8760. "
                     "breakeven_hours assumes taker fees on entry and exit on both venues and a constant spread.",
            "hyperliquid_error": hl_error,
        }

    def get_action_log(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.journal.tail(limit)

    # ---- orders ----------------------------------------------------------
    def place_order(self, symbol: str, side: str, size: float | str | None = None,
                    notional_usd: float | str | None = None, order_type: str = "market",
                    price: float | str | None = None, leverage: int | None = None,
                    reduce_only: bool = False, slippage: float | str | None = None,
                    dry_run: bool = False, confirmation_token: str | None = None,
                    reason: str | None = None, _approved_by_human: bool = False) -> dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        side = side.strip().lower()
        order_type = order_type.strip().lower()
        try:
            if side not in ("buy", "sell"):
                raise OrderRejected("side must be 'buy' or 'sell'")
            if order_type not in ("market", "limit"):
                raise OrderRejected("order_type must be 'market' or 'limit'")
            m = self._market(symbol)
            mark = D(m["mark_price"])

            if order_type == "limit":
                if price is None:
                    raise OrderRejected("A limit order needs a price.")
                tick, pmin = D(m["order_tick_price"]), D(m["order_min_price"])
                px = round_to_grid(D(str(price)), pmin, tick, ROUND_DOWN if side == "buy" else ROUND_UP)
            else:
                px = mark

            if size is None and notional_usd is None:
                raise OrderRejected("Give either size (base units) or notional_usd.")
            raw_size = D(str(size)) if size is not None else D(str(notional_usd)) / px
            prefix = "order_limit" if order_type == "limit" else "order_market"
            min_size = D(m[f"{prefix}_min_size"] if order_type == "limit" else m["order_market_min_size"])
            step = D(m[f"{prefix}_step_size"])
            sz = round_to_grid(raw_size, min_size, step)
            if sz < D(m["order_market_min_size"]):
                raise OrderRejected(f"Size {raw_size} is below the minimum {m['order_market_min_size']} for {symbol}.")
            max_size = D(m[f"{prefix}_max_size"])
            if sz > max_size:
                raise OrderRejected(f"Size {sz} is above the maximum {max_size} for {symbol}.")
            if not reduce_only and sz * px < D(m.get("order_min_notional") or "0"):
                raise OrderRejected(f"Order notional {sz * px:.2f} USD is below the {m['order_min_notional']} USD minimum.")

            policy = self.policies.load()
            slip = None
            if order_type == "market":
                slip = D(str(slippage)) if slippage is not None else min(D("0.005"), policy.max_slippage)
            state, account, _ = self._state()
            current_lev = self._current_leverage(symbol, m, account, state)
            state.current_leverage[symbol] = current_lev
            leverage_note = None
            if leverage is None and not reduce_only and current_lev > policy.max_leverage:
                # New risk would sit at the account's (higher) default leverage: cap it to the policy instead.
                leverage = policy.max_leverage
                leverage_note = f"Leverage on {symbol} will be lowered from {current_lev}x to the {leverage}x policy cap."
            intent = OrderIntent(symbol, side, order_type, sz, px, leverage, slip, reduce_only)
            decision = evaluate(policy, intent, state)
        except OrderRejected as e:
            self.journal.log("order_invalid", symbol=symbol, side=side, error=str(e), reason=reason)
            return {"status": "invalid", "error": str(e)}

        summary = {"symbol": symbol, "side": side, "type": order_type, "size": str(sz),
                   "price": str(px) if order_type == "limit" else None, "est_price": str(px),
                   "leverage": leverage, "reduce_only": reduce_only,
                   "slippage": str(slip) if slip is not None else None}
        if leverage_note:
            summary["note"] = leverage_note
        key = "|".join(str(summary[k]) for k in ("symbol", "side", "type", "size", "price", "leverage", "reduce_only"))

        if not decision.allowed:
            self.journal.log("order_blocked", order=summary, decision=decision.to_dict(), reason=reason)
            return {"status": "blocked", "order": summary, "guardrails": decision.to_dict(),
                    "message": "Blocked by guardrails. Tell the user which limit was hit; do not try to work around it."}

        needs_human = (policy.require_human_approval and decision.risk_increasing and not _approved_by_human
                       and decision.order_notional_usd >= policy.auto_approve_below_usd)
        if needs_human and not dry_run:
            approval_id = self.journal.add_approval({
                "order": summary, "notional_usd": _f(decision.order_notional_usd, 2), "reason": reason,
                "args": {"symbol": symbol, "side": side, "size": str(sz), "order_type": order_type,
                         "price": summary["price"], "leverage": leverage, "reduce_only": reduce_only,
                         "slippage": summary["slippage"]},
            })
            self.journal.log("order_pending_approval", approval_id=approval_id, order=summary,
                             decision=decision.to_dict(), reason=reason)
            return {"status": "pending_approval", "approval_id": approval_id, "order": summary,
                    "guardrails": decision.to_dict(),
                    "message": "Waiting for the user to approve this order in their Strike Agent Kit dashboard. "
                               "You cannot approve it yourself. Tell the user it is waiting, and use "
                               "get_order_status with this approval_id to see the outcome. Do not resubmit."}

        # The confirmation-token flow only applies when human approval is switched off.
        use_token = decision.needs_confirmation and not policy.require_human_approval
        if dry_run or (use_token and not (
                confirmation_token and self.journal.redeem_token(confirmation_token, key))):
            token = self.journal.issue_token(key) if use_token else None
            status = "preview" if dry_run else "needs_confirmation"
            self.journal.log(f"order_{status}", order=summary, decision=decision.to_dict(), reason=reason)
            msg = (("Preview only, nothing was sent." + (
                       " If the user wants it, place it with dry_run=false; it will then wait for their approval "
                       "in the dashboard." if needs_human else "")) if dry_run else
                   "This order is above the confirmation threshold. Show it to the user and only resend "
                   "with confirmation_token after they explicitly approve.")
            return {"status": status, "order": summary, "guardrails": decision.to_dict(),
                    "confirmation_token": token, "message": msg}

        return self._submit(summary, decision, reason)

    def _submit(self, summary: dict[str, Any], decision: Any, reason: str | None) -> dict[str, Any]:
        symbol = summary["symbol"]
        try:
            if summary["leverage"]:
                self.backend.set_leverage(symbol, int(summary["leverage"]))
            coid = f"agent-{uuid.uuid4().hex[:16]}"
            body: dict[str, Any] = {"client_order_id": coid, "symbol": symbol, "side": summary["side"],
                                    "type": summary["type"], "size": summary["size"]}
            if summary["type"] == "limit":
                body["price"] = summary["price"]
            if summary["slippage"]:
                body["slippage"] = summary["slippage"]
            if summary["reduce_only"]:
                body["reduce_only"] = True
            self.backend.place_order(body, self.settings.builder_fee_bps)
        except (StrikeAPIError, ValueError) as e:
            self.journal.log("order_error", order=summary, error=str(e), reason=reason)
            return {"status": "error", "order": summary, "error": str(e)}

        final = self._await_order(symbol, coid)
        status = (final or {}).get("Status", "unknown")
        result = {
            "status": "submitted" if status in ("filled", "open", "pending", "unknown") else "rejected",
            "exchange_status": status,
            "client_order_id": coid,
            "filled": (final or {}).get("Filled"),
            "avg_price": next((v for v in ((final or {}).get("AvgPrice"), (final or {}).get("Price"))
                               if v and D(str(v)) != 0), None),
            "close_reason": (final or {}).get("CloseReason") or None,
            "builder_fee_bps": self.settings.builder_fee_bps,
            "order": summary,
            "guardrails": decision.to_dict(),
        }
        if final is None:
            result["message"] = ("No order record yet. Strike drops some invalid orders silently; "
                                 "check positions before retrying, do not blindly resend.")
        self.journal.log("order_submitted", **{k: v for k, v in result.items() if k != "guardrails"}, reason=reason)
        return result

    def _await_order(self, symbol: str, coid: str) -> dict[str, Any] | None:
        deadline = time.time() + self.poll_timeout_s
        last = None
        while True:
            try:
                last = self.backend.get_order(symbol, coid)
            except StrikeAPIError:
                last = None
            if last and last.get("Status") not in ("pending",):
                return last
            if time.time() >= deadline:
                return last
            time.sleep(0.4)

    def close_position(self, symbol: str, dry_run: bool = False, reason: str | None = None,
                       slippage: float | str | None = None) -> dict[str, Any]:
        """Reduce-only market close. If the exchange rejects it for slippage, retry once at the
        policy's close_max_slippage; if that also fails, say plainly that the market is too thin."""
        symbol = self.normalize_symbol(symbol)
        pos = next((p for p in self.backend.get_positions() if p["symbol"] == symbol), None)
        if not pos or D(pos["size"]) == 0:
            return {"status": "noop", "message": f"No open position on {symbol}."}
        size = D(pos["size"])
        policy = self.policies.load()
        cap = max(policy.max_slippage, policy.close_max_slippage)
        first = D(str(slippage)) if slippage is not None else min(D("0.005"), policy.max_slippage)
        if first > cap:
            return {"status": "invalid", "error": f"Slippage {first} is above the close limit {cap} "
                                                  "(close_max_slippage in the guardrails file)."}

        def attempt(slip: D) -> dict[str, Any]:
            return self.place_order(symbol, "sell" if size > 0 else "buy", size=str(abs(size)),
                                    reduce_only=True, slippage=str(slip), dry_run=dry_run,
                                    reason=reason or "close_position")

        res = attempt(first)
        if dry_run or not _slippage_rejected(res):
            return res
        if first < cap:
            self.journal.log("close_retry", symbol=symbol, first_slippage=first, retry_slippage=cap)
            res = attempt(cap)
            res["retried_with_slippage"] = str(cap)
            if not _slippage_rejected(res):
                return res
        res["message"] = (f"Could not close {symbol}: no liquidity within {cap * 100:.2f}% of the mark price. "
                          "The position is still open. Tell the user; they can close it manually on Strike "
                          "or raise close_max_slippage in the guardrails file. Do not keep retrying.")
        return res

    def hedge_exposure(self, asset: str, spot_amount: float | str, hedge_ratio: float | str = 1.0,
                       dry_run: bool = False, confirmation_token: str | None = None,
                       reason: str | None = None) -> dict[str, Any]:
        """Bring the perp position to -(spot_amount * hedge_ratio)."""
        symbol = self.normalize_symbol(asset)
        ratio = D(str(hedge_ratio))
        if not D(0) <= ratio <= D("1.5"):
            return {"status": "invalid", "error": "hedge_ratio must be between 0 and 1.5"}
        target = -(D(str(spot_amount)) * ratio)
        pos = next((p for p in self.backend.get_positions() if p["symbol"] == symbol), None)
        current = D(pos["size"]) if pos else D(0)
        delta = target - current
        def fmt(x: Any) -> str:
            return format(D(str(x)).normalize(), "f")
        plan = {"symbol": symbol, "spot_amount": fmt(spot_amount), "hedge_ratio": fmt(ratio),
                "target_position": fmt(target), "current_position": fmt(current), "change": fmt(delta)}
        m = self.backend.get_markets().get(symbol)
        if m is None:
            return {"status": "invalid", "error": f"Unknown market {symbol}"}
        if abs(delta) < D(m["order_market_min_size"]):
            return {"status": "noop", "plan": plan, "message": "Already hedged within the minimum order size."}
        reducing = current != 0 and (delta > 0) != (current > 0) and abs(delta) <= abs(current)
        res = self.place_order(symbol, "buy" if delta > 0 else "sell", size=str(abs(delta)),
                               reduce_only=reducing, dry_run=dry_run, confirmation_token=confirmation_token,
                               reason=reason or f"hedge {spot_amount} {asset} at ratio {ratio}")
        res["hedge_plan"] = plan
        return res

    def cancel_open_orders(self, symbol: str | None = None) -> dict[str, Any]:
        sym = self.normalize_symbol(symbol) if symbol else None
        res = self.backend.cancel_all(sym)
        self.journal.log("cancel_all", symbol=sym)
        return {"status": "submitted", "symbol": sym, "response": res}

    # ---- human approval queue ---------------------------------------------
    def list_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{k: v for k, v in a.items() if k != "args"} for a in self.journal.approvals()[:limit]]

    def get_order_status(self, approval_id: str) -> dict[str, Any]:
        """For the agent: the state of an order waiting for (or decided by) the human."""
        a = self.journal.get_approval(approval_id)
        if a is None:
            return {"status": "unknown", "error": f"No approval request {approval_id}."}
        out = {"approval_id": approval_id, "status": a["status"], "order": a["order"]}
        if a.get("result"):
            out["result"] = a["result"]
        return out

    def decide_approval_by_human(self, approval_id: str, approve: bool) -> dict[str, Any]:
        """Only reachable from the local dashboard, never from an MCP tool."""
        a = self.journal.get_approval(approval_id)
        if a is None:
            return {"status": "unknown", "error": f"No approval request {approval_id}."}
        if a["status"] != "pending":
            return {"status": a["status"], "error": f"Request is already {a['status']}."}
        if not approve:
            self.journal.update_approval(approval_id, status="rejected", decided=time.time())
            self.journal.log("order_rejected_by_human", approval_id=approval_id, order=a["order"], reason=a.get("reason"))
            return {"status": "rejected"}
        self.journal.update_approval(approval_id, status="approved", decided=time.time())
        # Re-run the full pipeline: prices, positions or the kill switch may have changed since the request.
        result = self.place_order(**a["args"], dry_run=False, reason=a.get("reason"), _approved_by_human=True)
        final = {"submitted": "executed", "blocked": "blocked_on_approval"}.get(result["status"], "failed")
        self.journal.update_approval(approval_id, status=final,
                                     result={k: result.get(k) for k in ("status", "exchange_status", "filled",
                                                                        "avg_price", "close_reason", "error")
                                             if result.get(k) is not None}
                                     | ({"violations": result["guardrails"]["violations"]}
                                        if result["status"] == "blocked" else {}))
        return {"status": final, "result": result}

    def set_kill_switch_by_human(self, on: bool) -> dict[str, Any]:
        """Only reachable from the local dashboard, never from an MCP tool."""
        self.policies.set_kill_switch(on)
        self.journal.log("kill_switch_engaged" if on else "kill_switch_released", reason="human, via dashboard")
        return {"kill_switch": on}

    def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        self.policies.engage_kill_switch()
        self.journal.log("kill_switch_engaged", reason=reason)
        return {"status": "engaged", "message": "Kill switch is on. New risk is blocked; closing and reducing "
                                                "positions still works. Only a human can turn it off "
                                                f"(set kill_switch to false in {self.settings.guardrails_path})."}
