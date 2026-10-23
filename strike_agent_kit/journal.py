"""Append-only action log plus tiny state (day-start equity, confirmation tokens)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, D):
        return str(o)
    raise TypeError(type(o))


class Journal:
    def __init__(self, journal_path: Path, state_path: Path):
        self.journal_path = journal_path
        self.state_path = state_path
        journal_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- action log ------------------------------------------------------
    def log(self, event: str, **data: Any) -> dict[str, Any]:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **data}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)  # survives the folder being moved away
        with self.journal_path.open("a") as f:
            f.write(json.dumps(entry, default=_default) + "\n")
        return entry

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.journal_path.is_file():
            return []
        lines = self.journal_path.read_text().splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    # ---- state -----------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            return json.loads(self.state_path.read_text())
        return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2, default=_default))

    def day_pnl(self, equity: D, wallet: D | None = None, positions: dict[str, D] | None = None,
                collateral_price: D | None = None) -> D:
        """Trading PnL since the first reading of the current UTC day.

        Plain equity change would count deposits and withdrawals as profit or loss. So when
        the wallet balance moves while the open positions are unchanged since the previous
        reading, that move is booked as an external flow and left out. Wallet changes that
        coincide with a position change (realized PnL, fees) still count. Funding paid on
        unchanged positions is small and is excluded along with the flows.

        collateral_price: USD price of the collateral asset when margin is held in a volatile
        asset (e.g. ADA). The wallet move is then split in two: the revaluation of the
        collateral already held (price change at constant quantity) counts as PnL, and only
        the change in quantity (a deposit or withdrawal) is booked as a flow. Without it the
        wallet is treated as USD.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        state = self._read_state()
        pos = {k: str(v) for k, v in (positions or {}).items() if D(v) != 0}
        if state.get("day") != today:
            state.update(day=today, day_start_equity=str(equity), day_flows="0")
        elif wallet is not None and "last_wallet" in state and state.get("last_positions") == pos:
            last_wallet = D(state["last_wallet"])
            last_price = D(state["last_collateral_price"]) if state.get("last_collateral_price") else None
            revaluation = D(0)
            if collateral_price and last_price:
                revaluation = last_wallet * (collateral_price / last_price - 1)
            delta = wallet - last_wallet - revaluation
            if delta != 0:
                state["day_flows"] = str(D(state.get("day_flows", "0")) + delta)
        if wallet is not None:
            state["last_wallet"], state["last_positions"] = str(wallet), pos
            state["last_collateral_price"] = str(collateral_price) if collateral_price else None
        self._write_state(state)
        return equity - D(state["day_start_equity"]) - D(state.get("day_flows", "0"))

    def day_flows(self) -> D:
        """External wallet changes (deposits, withdrawals) excluded from today's PnL."""
        return D(self._read_state().get("day_flows", "0"))

    def issue_token(self, key: str, ttl_s: int = 300) -> str:
        import hashlib
        token = hashlib.sha256(f"{key}:{time.time()}".encode()).hexdigest()[:12]
        state = self._read_state()
        tokens = {t: v for t, v in state.get("tokens", {}).items() if v["exp"] > time.time()}
        tokens[token] = {"key": key, "exp": time.time() + ttl_s}
        state["tokens"] = tokens
        self._write_state(state)
        return token

    def redeem_token(self, token: str, key: str) -> bool:
        state = self._read_state()
        entry = state.get("tokens", {}).pop(token, None)
        self._write_state(state)
        return bool(entry and entry["key"] == key and entry["exp"] > time.time())

    # ---- approval queue --------------------------------------------------
    APPROVAL_TTL_S = 15 * 60

    def add_approval(self, entry: dict[str, Any]) -> str:
        approval_id = uuid.uuid4().hex[:8]
        state = self._read_state()
        approvals = state.setdefault("approvals", {})
        approvals[approval_id] = {"id": approval_id, "status": "pending", "created": time.time(),
                                  "expires": time.time() + self.APPROVAL_TTL_S, **entry}
        # keep the store small: drop resolved entries beyond the 50 most recent
        resolved = sorted((a for a in approvals.values() if a["status"] != "pending"), key=lambda a: a["created"])
        for a in resolved[:-50]:
            approvals.pop(a["id"], None)
        self._write_state(state)
        return approval_id

    def approvals(self) -> list[dict[str, Any]]:
        state = self._read_state()
        approvals = state.get("approvals", {})
        changed = False
        for a in approvals.values():
            if a["status"] == "pending" and a["expires"] < time.time():
                a["status"], changed = "expired", True
        if changed:
            self._write_state(state)
        return sorted(approvals.values(), key=lambda a: a["created"], reverse=True)

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return next((a for a in self.approvals() if a["id"] == approval_id), None)

    def update_approval(self, approval_id: str, **fields: Any) -> None:
        state = self._read_state()
        if approval_id in state.get("approvals", {}):
            state["approvals"][approval_id].update(fields)
            self._write_state(state)

    # ---- protection rules --------------------------------------------------
    def add_rule(self, rule: dict[str, Any]) -> str:
        rule_id = uuid.uuid4().hex[:6]
        state = self._read_state()
        rules = state.setdefault("rules", {})
        rules[rule_id] = {"id": rule_id, "created": time.time(), **rule}
        done = sorted((r for r in rules.values() if r["status"] not in ("proposed", "armed")),
                      key=lambda r: r["created"])
        for r in done[:-30]:
            rules.pop(r["id"], None)
        self._write_state(state)
        return rule_id

    def rules(self) -> list[dict[str, Any]]:
        state = self._read_state()
        rules = state.get("rules", {})
        changed = False
        for r in rules.values():
            if r["status"] in ("proposed", "armed") and r["expires"] < time.time():
                r["status"], changed = "expired", True
        if changed:
            self._write_state(state)
        return sorted(rules.values(), key=lambda r: r["created"], reverse=True)

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        return next((r for r in self.rules() if r["id"] == rule_id), None)

    def update_rule(self, rule_id: str, **fields: Any) -> None:
        state = self._read_state()
        if rule_id in state.get("rules", {}):
            state["rules"][rule_id].update(fields)
            self._write_state(state)

