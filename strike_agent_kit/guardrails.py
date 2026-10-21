"""Guardrails: the hard limits the agent cannot talk its way past.

The policy lives in a JSON file the human edits. It is re-read on every check,
so a change takes effect immediately. The agent can engage the kill switch but
has no tool to release it.

Principle: anything that *reduces* risk is always allowed, so the agent can
always get you out of a position, even with the kill switch on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal as D
from pathlib import Path
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "kill_switch": False,
    "allowed_markets": ["BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD"],
    "allowed_order_types": ["market", "limit"],
    "max_leverage": 5,
    "max_order_notional_usd": 2500,
    "max_position_notional_usd": 5000,
    "max_total_notional_usd": 10000,
    "daily_loss_limit_usd": 250,
    "max_slippage": 0.01,
    "close_max_slippage": 0.03,
    "confirm_above_usd": 1000,
    "require_human_approval": True,
    "auto_approve_below_usd": 0,
}


@dataclass
class Policy:
    kill_switch: bool = False
    allowed_markets: list[str] = field(default_factory=list)  # empty = every market
    allowed_order_types: list[str] = field(default_factory=lambda: ["market", "limit"])
    max_leverage: int = 5
    max_order_notional_usd: D = D("2500")
    max_position_notional_usd: D = D("5000")
    max_total_notional_usd: D = D("10000")
    daily_loss_limit_usd: D = D("250")
    max_slippage: D = D("0.01")
    # Closing reduces risk, so it may accept a wider price band than new orders. Used when a close at
    # normal slippage is rejected because the book is thin or moving fast.
    close_max_slippage: D = D("0.03")
    confirm_above_usd: D = D("1000")
    # New risk waits in a queue until a human approves it in the dashboard (the agent cannot).
    require_human_approval: bool = True
    auto_approve_below_usd: D = D("0")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        merged = {**DEFAULT_POLICY, **data}
        return cls(
            kill_switch=bool(merged["kill_switch"]),
            allowed_markets=[str(s) for s in merged["allowed_markets"]],
            allowed_order_types=[str(s) for s in merged["allowed_order_types"]],
            max_leverage=int(merged["max_leverage"]),
            max_order_notional_usd=D(str(merged["max_order_notional_usd"])),
            max_position_notional_usd=D(str(merged["max_position_notional_usd"])),
            max_total_notional_usd=D(str(merged["max_total_notional_usd"])),
            daily_loss_limit_usd=D(str(merged["daily_loss_limit_usd"])),
            max_slippage=D(str(merged["max_slippage"])),
            close_max_slippage=D(str(merged["close_max_slippage"])),
            confirm_above_usd=D(str(merged["confirm_above_usd"])),
            require_human_approval=bool(merged["require_human_approval"]),
            auto_approve_below_usd=D(str(merged["auto_approve_below_usd"])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: (float(v) if isinstance(v, D) else v) for k, v in asdict(self).items()}


# ---- changing the policy from the agent side ---------------------------------
# The agent may tighten a limit on its own. Anything that loosens a limit, or that could stop
# the human from getting out of a position, waits for a human in the dashboard.
LOWER_IS_TIGHTER = ("max_leverage", "max_order_notional_usd", "max_position_notional_usd",
                    "max_total_notional_usd", "max_slippage", "confirm_above_usd", "auto_approve_below_usd")
# Closing uses market orders and close_max_slippage. Even a "tighter" value there can trap the
# human in a thin market, so these always go to the human.
HUMAN_ONLY = ("allowed_order_types", "close_max_slippage")
CHANGEABLE = LOWER_IS_TIGHTER + ("daily_loss_limit_usd", "require_human_approval", "allowed_markets") + HUMAN_ONLY
LABELS = {
    "max_leverage": "Max leverage", "max_order_notional_usd": "Max per order",
    "max_position_notional_usd": "Max per market", "max_total_notional_usd": "Max total exposure",
    "max_slippage": "Max slippage", "close_max_slippage": "Close slippage",
    "confirm_above_usd": "Confirm in chat above", "auto_approve_below_usd": "Auto-approve below",
    "daily_loss_limit_usd": "Daily loss limit", "require_human_approval": "Human approval",
    "allowed_markets": "Allowed markets", "allowed_order_types": "Allowed order types",
}


def parse_change(name: str, value: Any) -> Any:
    """Validate one proposed value and return it in policy-file form. Raises ValueError."""
    if name == "kill_switch":
        raise ValueError("Use engage_kill_switch to turn the kill switch on. Only a human can turn it off.")
    if name not in CHANGEABLE:
        raise ValueError(f"Unknown guardrail {name!r}. Changeable: {', '.join(CHANGEABLE)}.")
    if name == "require_human_approval":
        if not isinstance(value, bool):
            raise ValueError("require_human_approval must be true or false.")
        return value
    if name in ("allowed_markets", "allowed_order_types"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{name} must be a list of strings.")
        if name == "allowed_markets":
            return sorted({(v.strip().upper() if "-" in v else f"{v.strip().upper()}-USD") for v in value})
        types = sorted({v.strip().lower() for v in value})
        if not types or not set(types) <= {"market", "limit"}:
            raise ValueError("allowed_order_types must be a non-empty subset of market, limit.")
        return types
    try:
        num = D(str(value))
    except Exception:
        raise ValueError(f"{name} must be a number.") from None
    if not num.is_finite() or num < 0:
        raise ValueError(f"{name} must be zero or more.")
    if name == "max_leverage":
        if num != num.to_integral_value() or num < 1:
            raise ValueError("max_leverage must be a whole number of at least 1.")
        return int(num)
    if name in ("max_slippage", "close_max_slippage") and not (D(0) < num <= D("0.2")):
        raise ValueError(f"{name} is a fraction between 0 and 0.2 (0.01 = 1%).")
    if name in ("max_order_notional_usd", "max_position_notional_usd", "max_total_notional_usd") and num == 0:
        raise ValueError(f"{name} must be above zero. Use engage_kill_switch to stop all new risk.")
    return float(num)


def classify_change(name: str, old: Any, new: Any) -> str:
    """'same', 'tighten' (the agent may apply it) or 'loosen' (a human must approve it)."""
    if name == "allowed_markets":
        old_set, new_set = set(old), set(new)  # empty list = every market
        if new_set == old_set:
            return "same"
        return "tighten" if new_set and (not old_set or new_set < old_set) else "loosen"
    if name in HUMAN_ONLY:
        same = sorted(old) == sorted(new) if isinstance(new, list) else D(str(old)) == D(str(new))
        return "same" if same else "loosen"
    if name == "require_human_approval":
        return "same" if bool(old) == new else ("tighten" if new else "loosen")
    old_d, new_d = D(str(old)), D(str(new))
    if old_d == new_d:
        return "same"
    if name == "daily_loss_limit_usd":  # 0 switches the limit off
        return "tighten" if new_d > 0 and (old_d == 0 or new_d < old_d) else "loosen"
    return "tighten" if new_d < old_d else "loosen"


class PolicyStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Policy:
        if not self.path.is_file():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(DEFAULT_POLICY, indent=2) + "\n")
        return Policy.from_dict(json.loads(self.path.read_text()))

    def set_kill_switch(self, on: bool) -> None:
        data = json.loads(self.path.read_text()) if self.path.is_file() else dict(DEFAULT_POLICY)
        data["kill_switch"] = bool(on)
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def engage_kill_switch(self) -> None:
        self.set_kill_switch(True)

    def update(self, changes: dict[str, Any]) -> None:
        """Write already-validated values (see parse_change) into the policy file."""
        data = json.loads(self.path.read_text()) if self.path.is_file() else dict(DEFAULT_POLICY)
        data.update(changes)
        Policy.from_dict(data)  # fail before writing if the result is not a valid policy
        self.path.write_text(json.dumps(data, indent=2) + "\n")


@dataclass
class OrderIntent:
    symbol: str
    side: str  # "buy" | "sell"
    order_type: str
    size: D  # base units, already rounded to the market step
    price: D  # mark price (market) or limit price, for notional
    leverage: int | None = None
    slippage: D | None = None
    reduce_only: bool = False


@dataclass
class AccountState:
    positions: dict[str, D]  # symbol -> signed size
    marks: dict[str, D]  # symbol -> mark price
    day_pnl_usd: D  # trading PnL since start of UTC day, excluding deposits/withdrawals
    current_leverage: dict[str, int] = field(default_factory=dict)


@dataclass
class Violation:
    rule: str
    message: str


@dataclass
class Decision:
    allowed: bool
    risk_increasing: bool
    order_notional_usd: D
    position_after: D
    position_notional_after_usd: D
    total_notional_after_usd: D
    violations: list[Violation]
    needs_confirmation: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk_increasing": self.risk_increasing,
            "order_notional_usd": float(round(self.order_notional_usd, 2)),
            "position_after": str(self.position_after),
            "position_notional_after_usd": float(round(self.position_notional_after_usd, 2)),
            "total_notional_after_usd": float(round(self.total_notional_after_usd, 2)),
            "violations": [asdict(v) for v in self.violations],
            "needs_confirmation": self.needs_confirmation,
        }


def evaluate(policy: Policy, intent: OrderIntent, state: AccountState) -> Decision:
    current = state.positions.get(intent.symbol, D(0))
    delta = intent.size if intent.side == "buy" else -intent.size
    if intent.reduce_only:
        # clamp: a reduce-only order can at most close the position
        if current == 0 or (current > 0) == (delta > 0):
            delta = D(0)
        else:
            delta = max(-abs(current), min(abs(current), delta))
    after = current + delta
    risk_increasing = abs(after) > abs(current) or (after != 0 and (after > 0) != (current > 0) and current != 0)

    mark = state.marks.get(intent.symbol, intent.price)
    order_notional = abs(delta) * intent.price
    position_notional_after = abs(after) * mark
    total_after = sum(
        (abs(size) * state.marks.get(sym, D(0)) for sym, size in state.positions.items() if sym != intent.symbol),
        D(0),
    ) + position_notional_after

    v: list[Violation] = []
    if risk_increasing:
        if policy.kill_switch:
            v.append(Violation("kill_switch", "Kill switch is on. Only risk-reducing orders are allowed."))
        if policy.allowed_markets and intent.symbol not in policy.allowed_markets:
            v.append(Violation("allowed_markets", f"{intent.symbol} is not in the allowed markets list."))
        if policy.daily_loss_limit_usd > 0 and state.day_pnl_usd <= -policy.daily_loss_limit_usd:
            v.append(Violation(
                "daily_loss_limit",
                f"Daily loss {state.day_pnl_usd:.2f} USD has hit the limit of -{policy.daily_loss_limit_usd} USD.",
            ))
        if order_notional > policy.max_order_notional_usd:
            v.append(Violation(
                "max_order_notional",
                f"Order notional {order_notional:.2f} USD exceeds the {policy.max_order_notional_usd} USD limit.",
            ))
        if position_notional_after > policy.max_position_notional_usd:
            v.append(Violation(
                "max_position_notional",
                f"Position would be {position_notional_after:.2f} USD, above the "
                f"{policy.max_position_notional_usd} USD per-market limit.",
            ))
        if total_after > policy.max_total_notional_usd:
            v.append(Violation(
                "max_total_notional",
                f"Total exposure would be {total_after:.2f} USD, above the {policy.max_total_notional_usd} USD limit.",
            ))
        effective_lev = intent.leverage or state.current_leverage.get(intent.symbol)
        if effective_lev is not None and effective_lev > policy.max_leverage:
            v.append(Violation(
                "max_leverage", f"Leverage {effective_lev}x exceeds the {policy.max_leverage}x limit."))
    elif intent.leverage is not None and intent.leverage > policy.max_leverage:
        v.append(Violation("max_leverage", f"Leverage {intent.leverage}x exceeds the {policy.max_leverage}x limit."))

    if intent.order_type not in policy.allowed_order_types:
        v.append(Violation("allowed_order_types", f"Order type {intent.order_type!r} is not allowed."))
    slip_cap = max(policy.max_slippage, policy.close_max_slippage) if intent.reduce_only else policy.max_slippage
    if intent.slippage is not None and intent.slippage > slip_cap:
        v.append(Violation("max_slippage", f"Slippage {intent.slippage} exceeds the {slip_cap} limit."))
    if delta == 0:
        v.append(Violation("no_effect", "This order would not change the position."))

    return Decision(
        allowed=not v,
        risk_increasing=risk_increasing,
        order_notional_usd=order_notional,
        position_after=after,
        position_notional_after_usd=position_notional_after,
        total_notional_after_usd=total_after,
        violations=v,
        needs_confirmation=risk_increasing and order_notional > policy.confirm_above_usd,
    )
