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
from .guardrails import LABELS, AccountState, OrderIntent, PolicyStore, classify_change, evaluate, parse_change
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
        # The MCP server, the dashboard and the rule watcher run in different threads of one process.
        self.lock = threading.RLock()
        # Called with (rule, outcome) whenever a protection rule fires, e.g. a desktop notification.
        self.on_rule_fired: list[Any] = []
        self.watcher_error: str | None = None

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
        marks = {s: D(m["mark_price"]) for s, m in markets.items()}
        coll_price = None
        if self.settings.collateral_asset:
            coll_price = marks.get(self.normalize_symbol(self.settings.collateral_asset))
            if not coll_price:
                raise OrderRejected(f"Collateral asset {self.settings.collateral_asset} has no Strike market to price it.")
        state = AccountState(
            positions=sizes,
            marks=marks,
            day_pnl_usd=self.journal.day_pnl(equity, D(account["wallet_balance"]), sizes, coll_price),
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
                "best_bid": m.get("bid1_price"), "best_ask": m.get("ask1_price"),
                "book_gap_pct": self._book_gap_pct(m),
            })
        return out

    @staticmethod
    def _book_gap_pct(m: dict[str, Any]) -> float | None:
        """How far the middle of Strike's top of book sits from the mark price, in %."""
        try:
            mark, bid, ask = D(m["mark_price"]), D(m["bid1_price"]), D(m["ask1_price"])
        except (KeyError, TypeError, ArithmeticError):
            return None
        if mark <= 0 or bid <= 0 or ask <= 0:
            return None
        return _f(((bid + ask) / 2 / mark - 1) * 100, 2)

    @staticmethod
    def _thin_book_note(m: dict[str, Any], side: str, slip: D | None) -> str | None:
        """Warn when the price a market order would hit is further from the mark than the slippage allows."""
        try:
            mark = D(m["mark_price"])
            hit = D(m["bid1_price"] if side == "sell" else m["ask1_price"])
        except (KeyError, TypeError, ArithmeticError):
            return None
        if mark <= 0 or hit <= 0 or slip is None:
            return None
        gap = abs(hit / mark - 1)
        if gap <= slip:
            return None
        return (f"Strike's best {'bid' if side == 'sell' else 'ask'} ({hit}) is {gap * 100:.1f}% from the mark "
                f"({mark}); with {slip * 100:.2f}% slippage this market order will likely be cancelled.")

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
        Defaults to STRIKE_COLLATERAL_ASSET.
        """
        collateral_asset = collateral_asset or self.settings.collateral_asset
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
        # Collateral revaluation counts toward day PnL (and the daily loss limit) only when the kit
        # knows the margin asset; otherwise the live day PnL would not see it either.
        tracked = bool(coll) and self.normalize_symbol(collateral_asset or "") == self.normalize_symbol(
            self.settings.collateral_asset or "")
        day_after = state.day_pnl_usd + perp_pnl + (collateral_pnl if tracked else D(0))
        liquidated = bool(rows) and equity_after <= maint_after
        warnings = []
        if liquidated:
            warnings.append("Account would be at or below maintenance margin: liquidation risk.")
        if policy.daily_loss_limit_usd > 0 and day_after <= -policy.daily_loss_limit_usd:
            warnings.append(f"Daily loss limit ({policy.daily_loss_limit_usd} USD) would trip: "
                            "new risk would be blocked, closes still allowed.")
        unhedged = [r for r in spot_rows if "hedged_pct" in r and r["hedged_pct"] < 50 and r["pnl_usd"] < 0]
        for r in unhedged:
            warnings.append(f"{r['asset']} spot is {r['hedged_pct']}% hedged; request_hedge can prepare a hedge for the user to execute.")
        if coll and collateral_pnl < 0 and any(r.get("asset") == coll["asset"] for r in spot_rows):
            warnings.append(f"Margin is held in {coll['asset']}, which adds to the same exposure as the spot holding.")
        if coll and not tracked:
            warnings.append(f"STRIKE_COLLATERAL_ASSET is not set to {coll['asset']}, so the daily loss limit "
                            "does not see collateral losses. Set it to count them.")

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
                "note": "Use propose_guardrail_change to change a limit: tightening applies at once, loosening "
                        "waits for the human in the dashboard. The agent can engage the kill switch, "
                        "but only a human can release it."}

    def propose_guardrail_change(self, changes: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        """Tightening applies immediately. Loosening (or anything that could block an exit) waits for a human."""
        if not isinstance(changes, dict) or not changes:
            return {"status": "invalid", "error": "Pass changes as {guardrail: new value}, e.g. {\"max_leverage\": 3}."}
        policy = self.policies.load().to_dict()
        parsed, errors = {}, []
        for name, value in changes.items():
            try:
                parsed[name] = parse_change(name, value)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            self.journal.log("guardrail_change_invalid", requested=changes, error=" ".join(errors), reason=reason)
            return {"status": "invalid", "error": " ".join(errors), "message": "Nothing was changed."}

        tighten, loosen = {}, {}
        for name, new in parsed.items():
            kind = classify_change(name, policy[name], new)
            if kind != "same":
                (tighten if kind == "tighten" else loosen)[name] = {"label": LABELS[name], "from": policy[name], "to": new}
        if not tighten and not loosen:
            return {"status": "unchanged", "message": "Those values are already in force."}

        out: dict[str, Any] = {}
        if tighten:
            self.policies.update({k: v["to"] for k, v in tighten.items()})
            self.journal.log("guardrails_tightened", changes=tighten, reason=reason)
            out["applied"] = tighten
        if loosen:
            approval_id = self.journal.add_approval({"kind": "guardrail_change", "changes": loosen, "reason": reason})
            self.journal.log("guardrail_change_pending", approval_id=approval_id, changes=loosen, reason=reason)
            out.update(approval_id=approval_id, pending=loosen)
        out["status"] = "pending_approval" if loosen and not tighten else (
            "partially_applied" if loosen else "applied")
        out["message"] = " ".join(m for m in (
            "Tighter limits are in force now." if tighten else "",
            "Loosening a limit waits for the user to approve it in their dashboard; you cannot approve it. Tell them, "
            "and check the outcome with get_order_status. Do not resubmit or route around the current limit."
            if loosen else "") if m)
        return out

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
                    reason: str | None = None, _approved_by_human: bool = False,
                    _request_only: bool = False, _close: bool = False) -> dict[str, Any]:
        """_request_only: the caller is the agent (MCP). Nothing is executed then: a non-preview call always
        becomes an approval card, and only the human's click in the dashboard sends it to Strike."""
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
        book_note = self._thin_book_note(m, side, slip) if order_type == "market" else None
        if leverage_note or book_note:
            summary["note"] = " ".join(n for n in (leverage_note, book_note) if n)
        key = "|".join(str(summary[k]) for k in ("symbol", "side", "type", "size", "price", "leverage", "reduce_only"))

        if not decision.allowed:
            self.journal.log("order_blocked", order=summary, decision=decision.to_dict(), reason=reason)
            return {"status": "blocked", "order": summary, "guardrails": decision.to_dict(),
                    "message": "Blocked by guardrails. Tell the user which limit was hit; do not try to work around it."}

        needs_human = not _approved_by_human and (_request_only or (
            policy.require_human_approval and decision.risk_increasing
            and decision.order_notional_usd >= policy.auto_approve_below_usd))
        if needs_human and not dry_run:
            approval_id = self.journal.add_approval({
                "order": summary, "notional_usd": _f(decision.order_notional_usd, 2), "reason": reason,
                "close": _close,
                "args": {"symbol": symbol, "side": side, "size": str(sz), "order_type": order_type,
                         "price": summary["price"], "leverage": leverage, "reduce_only": reduce_only,
                         "slippage": summary["slippage"]},
            })
            self.journal.log("order_pending_approval", approval_id=approval_id, order=summary,
                             decision=decision.to_dict(), reason=reason)
            return {"status": "pending_approval", "approval_id": approval_id, "order": summary,
                    "guardrails": decision.to_dict(),
                    "message": "Sent to the user's Strike Agent Kit dashboard. Nothing has been traded: the user "
                               "reviews it there and executes it with one click, or rejects it. You cannot approve "
                               "it. Tell the user it is waiting for them, and use get_order_status with this "
                               "approval_id to see the outcome. Do not resubmit."}

        # The confirmation-token flow only applies when human approval is switched off, and never to
        # an order the human already approved (an approval card or an armed protection rule).
        use_token = (decision.needs_confirmation and not policy.require_human_approval and not _approved_by_human
                     and not _request_only)
        if dry_run or (use_token and not (
                confirmation_token and self.journal.redeem_token(confirmation_token, key))):
            token = self.journal.issue_token(key) if use_token else None
            status = "preview" if dry_run else "needs_confirmation"
            self.journal.log(f"order_{status}", order=summary, decision=decision.to_dict(), reason=reason)
            msg = (("Preview only, nothing was sent." + (
                       " If the user wants it, send it to their dashboard with dry_run=false; they execute it "
                       "there with one click." if needs_human else "")) if dry_run else
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
                       slippage: float | str | None = None, _request_only: bool = False) -> dict[str, Any]:
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
                                    reason=reason or "close_position", _request_only=_request_only,
                                    _close=_request_only)

        res = attempt(first)
        if _request_only:  # the human executes it; on approval the close (with its retry) runs then
            return res
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
                       reason: str | None = None, _approved_by_human: bool = False,
                       _request_only: bool = False) -> dict[str, Any]:
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
                               reason=reason or f"hedge {spot_amount} {asset} at ratio {ratio}",
                               _approved_by_human=_approved_by_human, _request_only=_request_only)
        res["hedge_plan"] = plan
        return res

    def cancel_open_orders(self, symbol: str | None = None) -> dict[str, Any]:
        sym = self.normalize_symbol(symbol) if symbol else None
        res = self.backend.cancel_all(sym)
        self.journal.log("cancel_all", symbol=sym)
        return {"status": "submitted", "symbol": sym, "response": res}

    # ---- protection rules -------------------------------------------------
    # A rule is "if <market> trades below/above <price>, do <action>". The agent can only propose
    # one; the human arms it once in the dashboard. After that the watcher fires it by itself, at
    # most once, through the same guardrailed order pipeline (kill switch and limits still apply).
    MAX_ACTIVE_RULES = 5
    MAX_RULE_HOURS = 168

    @staticmethod
    def _rule_text(r: dict[str, Any]) -> str:
        base = r["symbol"].replace("-USD", "")
        when = f"If {base} {'falls below' if r['condition'] == 'below' else 'rises above'} ${D(r['trigger_price']):,}"
        if r["action"] == "hedge":
            ratio = D(str(r["hedge_ratio"]))
            what = f"hedge {D(r['spot_amount']):,} {base}" + ("" if ratio == 1 else f" at {format(ratio.normalize(), 'f')}:1")
        else:
            what = f"close my {base} position"
        return f"{when}: {what}"

    @staticmethod
    def _met(condition: str, mark: D, trigger: D) -> bool:
        return mark <= trigger if condition == "below" else mark >= trigger

    def propose_protection_rule(self, asset: str, condition: str, trigger_price: float | str, action: str = "hedge",
                                spot_amount: float | str | None = None, hedge_ratio: float | str = 1.0,
                                expires_in_hours: float = 24, reason: str | None = None) -> dict[str, Any]:
        symbol = self.normalize_symbol(asset)
        condition, action = str(condition).strip().lower(), str(action).strip().lower()
        try:
            m = self._market(symbol)
            if condition not in ("below", "above"):
                raise OrderRejected("condition must be 'below' or 'above'.")
            if action not in ("hedge", "close"):
                raise OrderRejected("action must be 'hedge' (short to cover spot holdings) or 'close' (close the position).")
            trigger = D(str(trigger_price))
            if trigger <= 0:
                raise OrderRejected("trigger_price must be above 0.")
            hours = float(expires_in_hours)
            if not 0 < hours <= self.MAX_RULE_HOURS:
                raise OrderRejected(f"expires_in_hours must be between 0 and {self.MAX_RULE_HOURS}.")
            ratio = D(str(hedge_ratio))
            if action == "hedge":
                if spot_amount is None or D(str(spot_amount)) <= 0:
                    raise OrderRejected("A hedge rule needs spot_amount: how much of the asset the user holds.")
                if not D(0) < ratio <= D("1.5"):
                    raise OrderRejected("hedge_ratio must be above 0 and at most 1.5.")
            elif not any(p["symbol"] == symbol and D(p["size"]) != 0 for p in self.backend.get_positions()):
                raise OrderRejected(f"There is no open {symbol} position to close.")
            active = [r for r in self.journal.rules() if r["status"] in ("proposed", "armed")]
            if len(active) >= self.MAX_ACTIVE_RULES:
                raise OrderRejected(f"There are already {len(active)} active rules (max {self.MAX_ACTIVE_RULES}). "
                                    "Cancel one first.")
        except (OrderRejected, ArithmeticError, ValueError) as e:
            self.journal.log("protection_rule_invalid", symbol=symbol, error=str(e), reason=reason)
            return {"status": "invalid", "error": str(e), "message": "No rule was created."}

        mark = D(m["mark_price"])
        rule: dict[str, Any] = {
            "status": "proposed", "symbol": symbol, "condition": condition,
            "trigger_price": format(trigger.normalize(), "f"), "action": action,
            "spot_amount": format(D(str(spot_amount)).normalize(), "f") if action == "hedge" else None,
            "hedge_ratio": format(ratio.normalize(), "f") if action == "hedge" else None,
            "hours": hours, "reason": reason, "mark_at_proposal": str(mark),
            "expires": time.time() + self.journal.APPROVAL_TTL_S,
        }
        rule["text"] = self._rule_text(rule)
        already = self._met(condition, mark, trigger)
        rule_id = self.journal.add_rule(rule)
        approval_id = self.journal.add_approval({"kind": "protection_rule", "rule_id": rule_id, "rule": rule,
                                                 "reason": reason})
        self.journal.update_rule(rule_id, approval_id=approval_id)

        # What it would do at today's price, so the agent can show the user the size and the guardrail check.
        preview = None
        if action == "hedge":
            p = self.hedge_exposure(symbol, rule["spot_amount"], rule["hedge_ratio"], dry_run=True,
                                    reason=f"preview for protection rule {rule_id}")
            preview = {k: p.get(k) for k in ("status", "order", "hedge_plan") if p.get(k) is not None}
            book = next((n for n in [(p.get("order") or {}).get("note") or ""] if "likely be cancelled" in n), None)
            if book:
                book = book[book.index("Strike's best"):]
                preview["book_warning"] = book
                self.journal.update_approval(approval_id, book_warning=book)
            if p.get("guardrails"):
                preview["guardrails_now"] = {k: p["guardrails"].get(k) for k in ("allowed", "violations")}
            if p.get("status") == "blocked":
                warn = [v.get("message") for v in p["guardrails"].get("violations", [])]
                self.journal.update_rule(rule_id, preview_violations=warn)
                self.journal.update_approval(approval_id, preview_violations=warn)
        self.journal.log("protection_rule_proposed", rule_id=rule_id, approval_id=approval_id, rule=rule["text"],
                         mark=str(mark), reason=reason)
        dist = (trigger / mark - 1) * 100 if mark else D(0)
        return {
            "status": "pending_approval", "rule_id": rule_id, "approval_id": approval_id, "rule": rule["text"],
            "mark_now": str(mark), "distance_to_trigger_pct": _f(dist, 2),
            "fires_immediately_on_approval": already, "expires_after_arming_hours": hours,
            "preview_at_current_price": preview,
            "message": (("At today's price the guardrails would block this hedge, so the rule would fire and be "
                         "blocked. Tell the user before they arm it. " if preview and preview.get("status") == "blocked"
                         else "") +
                        ("Strike's order book is far from the mark on this market, so the hedge would likely be "
                         "cancelled for slippage if it fired now. Tell the user. " if preview and preview.get("book_warning")
                         else "") +
                        "Waiting for the user to arm this rule in their dashboard; you cannot arm it. Once armed, "
                        "the kit watches the price and acts by itself, once, within the guardrails. "
                        + ("The condition is already met, so it will fire as soon as they approve. Say so. "
                           if already else "")
                        + "Check the outcome with list_protection_rules."),
        }

    def list_protection_rules(self, limit: int = 10) -> list[dict[str, Any]]:
        rules = self.journal.rules()[:limit]
        marks: dict[str, Any] = {}
        if any(r["status"] in ("proposed", "armed") for r in rules):
            try:
                marks = {s: D(mk["mark_price"]) for s, mk in self.backend.get_markets().items()}
            except Exception:  # noqa: BLE001  a read failure shouldn't hide the rules
                marks = {}
        out = []
        for r in rules:
            row = {k: r.get(k) for k in ("id", "status", "text", "symbol", "condition", "trigger_price", "action",
                                         "spot_amount", "hedge_ratio", "reason", "created", "armed_at", "expires",
                                         "triggered_at", "trigger_mark", "result", "approval_id", "hours",
                                         "mark_at_proposal", "cancelled_by")}
            mark = marks.get(r["symbol"])
            if r["status"] in ("proposed", "armed") and mark:
                row["mark_now"] = str(mark)
                row["distance_to_trigger_pct"] = _f((D(r["trigger_price"]) / mark - 1) * 100, 2)
            out.append(row)
        return out

    def cancel_protection_rule(self, rule_id: str, by: str = "agent", reason: str | None = None) -> dict[str, Any]:
        r = self.journal.get_rule(rule_id)
        if r is None:
            return {"status": "unknown", "error": f"No protection rule {rule_id}."}
        if r["status"] not in ("proposed", "armed"):
            return {"status": r["status"], "error": f"Rule is already {r['status']}."}
        self.journal.update_rule(rule_id, status="cancelled", decided=time.time(), cancelled_by=by)
        if r["status"] == "proposed" and r.get("approval_id"):
            self.journal.update_approval(r["approval_id"], status="cancelled", decided=time.time())
        self.journal.log("protection_rule_cancelled", rule_id=rule_id, rule=r["text"], by=by, reason=reason)
        return {"status": "cancelled", "rule_id": rule_id, "rule": r["text"]}

    def _arm_rule_by_human(self, approval: dict[str, Any], approve: bool) -> dict[str, Any]:
        rule_id = approval["rule_id"]
        r = self.journal.get_rule(rule_id)
        if r is None or r["status"] != "proposed":
            self.journal.update_approval(approval["id"], status="expired", decided=time.time())
            return {"status": "expired", "error": "The rule is no longer waiting for approval."}
        if not approve:
            self.journal.update_approval(approval["id"], status="rejected", decided=time.time())
            self.journal.update_rule(rule_id, status="rejected", decided=time.time())
            self.journal.log("protection_rule_rejected", rule_id=rule_id, approval_id=approval["id"],
                             rule=r["text"], reason=r.get("reason"))
            return {"status": "rejected"}
        now = time.time()
        self.journal.update_rule(rule_id, status="armed", armed_at=now, expires=now + float(r["hours"]) * 3600)
        self.journal.update_approval(approval["id"], status="armed", decided=now)
        self.journal.log("protection_rule_armed", rule_id=rule_id, approval_id=approval["id"], rule=r["text"],
                         reason=r.get("reason"))
        fired = self.check_protection_rules()  # fires at once if the condition is already met
        return {"status": "armed", "rule_id": rule_id, "fired": fired or None}

    def check_protection_rules(self) -> list[dict[str, Any]]:
        """Evaluate armed rules against current marks and fire the ones whose condition is met.
        Called by the watcher thread every few seconds, and right after a rule is armed."""
        armed = [r for r in self.journal.rules() if r["status"] == "armed"]
        if not armed:
            return []
        markets = self.backend.get_markets()
        fired = []
        for r in armed:
            m = markets.get(r["symbol"])
            mark = D(m["mark_price"]) if m and m.get("mark_price") else D(0)
            if mark <= 0:
                continue
            self.journal.update_rule(r["id"], last_mark=str(mark), last_checked=time.time())
            if not self._met(r["condition"], mark, D(r["trigger_price"])):
                continue
            fired.append(self._fire_rule(r, mark))
        return fired

    def _fire_rule(self, r: dict[str, Any], mark: D) -> dict[str, Any]:
        # Mark it first: a rule fires at most once, even if the order fails.
        self.journal.update_rule(r["id"], status="triggered", triggered_at=time.time(), trigger_mark=str(mark))
        self.journal.log("protection_rule_triggered", rule_id=r["id"], rule=r["text"], mark=str(mark))
        armed_at = time.strftime("%H:%M", time.localtime(r.get("armed_at") or time.time()))
        why = f"Protection rule {r['id']} ({r['text']}), armed by you at {armed_at}"
        try:
            if r["action"] == "hedge":
                res = self.hedge_exposure(r["symbol"], r["spot_amount"], r["hedge_ratio"], dry_run=False,
                                          reason=why, _approved_by_human=True)
            else:
                res = self.close_position(r["symbol"], reason=why)
        except Exception as e:  # noqa: BLE001  never let one rule kill the watcher
            res = {"status": "error", "error": str(e)}
        st = res.get("status")
        outcome = ("executed" if st == "submitted" and res.get("exchange_status") in ("filled", "open", "unknown")
                   else "noop" if st == "noop" else "blocked" if st == "blocked" else "failed")
        summary = {k: res.get(k) for k in ("status", "exchange_status", "filled", "avg_price", "close_reason",
                                           "error", "message") if res.get(k) is not None}
        if res.get("order"):
            summary["order"] = {k: res["order"].get(k) for k in ("symbol", "side", "size", "reduce_only")}
        if st == "blocked":
            summary["violations"] = [v.get("message") for v in (res.get("guardrails") or {}).get("violations", [])]
        self.journal.update_rule(r["id"], status=outcome, result=summary)
        self.journal.log(f"protection_rule_{outcome}", rule_id=r["id"], rule=r["text"], mark=str(mark),
                         result=summary, reason=why)
        out = {"rule_id": r["id"], "rule": r["text"], "mark": str(mark), "outcome": outcome, "result": summary}
        for cb in self.on_rule_fired:
            try:
                cb(r, out)
            except Exception:  # noqa: BLE001
                pass
        return out

    # ---- human approval queue ---------------------------------------------
    def list_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{k: v for k, v in a.items() if k != "args"} for a in self.journal.approvals()[:limit]]

    def get_order_status(self, approval_id: str) -> dict[str, Any]:
        """For the agent: the state of an order waiting for (or decided by) the human."""
        a = self.journal.get_approval(approval_id)
        if a is None:
            return {"status": "unknown", "error": f"No approval request {approval_id}."}
        out = {"approval_id": approval_id, "status": a["status"]}
        if a.get("kind") == "protection_rule":
            rule = self.journal.get_rule(a["rule_id"]) or {}
            return {**out, "rule_id": a["rule_id"], "rule": rule.get("text"), "rule_status": rule.get("status"),
                    "result": rule.get("result")}
        out.update({"changes": a["changes"]} if a.get("kind") == "guardrail_change" else {"order": a["order"]})
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
        if a.get("kind") == "protection_rule":
            return self._arm_rule_by_human(a, approve)
        if a.get("kind") == "guardrail_change":
            if not approve:
                self.journal.update_approval(approval_id, status="rejected", decided=time.time())
                self.journal.log("guardrail_change_rejected", approval_id=approval_id, changes=a["changes"],
                                 reason=a.get("reason"))
                return {"status": "rejected"}
            self.policies.update({k: v["to"] for k, v in a["changes"].items()})
            self.journal.update_approval(approval_id, status="applied", decided=time.time())
            self.journal.log("guardrails_changed_by_human", approval_id=approval_id, changes=a["changes"],
                             reason=a.get("reason"))
            return {"status": "applied", "changes": a["changes"]}
        if not approve:
            self.journal.update_approval(approval_id, status="rejected", decided=time.time())
            self.journal.log("order_rejected_by_human", approval_id=approval_id, order=a["order"], reason=a.get("reason"))
            return {"status": "rejected"}
        self.journal.update_approval(approval_id, status="approved", decided=time.time())
        if a.get("close"):
            # Close whatever is open now (with the slippage retry); closing is never blocked.
            result = self.close_position(a["args"]["symbol"], reason=a.get("reason"))
        else:
            # Re-run the full pipeline: prices, positions or the kill switch may have changed since the request.
            result = self.place_order(**a["args"], dry_run=False, reason=a.get("reason"), _approved_by_human=True)
        final = {"submitted": "executed", "blocked": "blocked_on_approval", "noop": "noop"}.get(result["status"], "failed")
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
