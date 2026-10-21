"""Thin Strike v2 REST client. Returns Strike's own JSON shapes."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx

from .config import Settings
from .signing import load_signing_key, sign_headers


class StrikeAPIError(Exception):
    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"{status} {message} ({path})")
        self.status = status
        self.message = message
        self.path = path


class StrikeBackend(Protocol):
    """What the agent service needs. Implemented by StrikeClient and MockStrike."""

    def get_markets(self) -> dict[str, dict[str, Any]]: ...
    def get_account(self) -> dict[str, Any]: ...
    def get_positions(self) -> list[dict[str, Any]]: ...
    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...
    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]: ...
    def place_order(self, order: dict[str, Any], builder_fee_bps: int | None = None) -> dict[str, Any]: ...
    def get_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None: ...
    def cancel_all(self, symbol: str | None = None) -> dict[str, Any]: ...


class StrikeClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.base_url = settings.base_url.rstrip("/")
        self.public_key = settings.api_public_key
        self._key = load_signing_key(settings.api_private_key) if settings.api_private_key else None
        self.http = http or httpx.Client(timeout=10.0)
        self._clock_offset_ms: int | None = None

    # ---- plumbing -------------------------------------------------------
    def sync_clock(self) -> None:
        t0 = time.time() * 1000
        server = self.http.get(f"{self.base_url}/v2/time").json()["serverTime"]
        t1 = time.time() * 1000
        self._clock_offset_ms = int(server - (t0 + t1) / 2)

    def _timestamp(self) -> int:
        if self._clock_offset_ms is None:
            self.sync_clock()
        # 1 s behind server time: a timestamp in the future is rejected.
        return int((time.time() * 1000 + (self._clock_offset_ms or 0) - 1000) // 1000)

    def _request(self, method: str, path_with_query: str, body: Any = None, *, signed: bool = True,
                 builder_fee_bps: int | None = None) -> Any:
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers: dict[str, str] = {}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if signed:
            if not (self._key and self.public_key):
                raise RuntimeError("API wallet keys are not configured")
            headers.update(sign_headers(self._key, self.public_key, method, path_with_query, raw, self._timestamp()))
        if builder_fee_bps is not None:
            headers["X-Builder-Fee-Bps"] = str(builder_fee_bps)

        res = self.http.request(method, f"{self.base_url}{path_with_query}", content=raw, headers=headers)
        text = res.text
        data = json.loads(text) if text else None
        if res.status_code >= 400:
            msg = (data or {}).get("error") or (data or {}).get("msg") or text
            if res.status_code == 401 and "timestamp" in str(msg):
                self._clock_offset_ms = None  # resync next time
            raise StrikeAPIError(res.status_code, str(msg), path_with_query)
        return data

    # ---- reads ----------------------------------------------------------
    def get_markets(self) -> dict[str, dict[str, Any]]:
        return self._request("GET", "/v2/markets", signed=False)["markets"]

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def get_positions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/positions").get("positions") or []

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        q = f"?symbol={symbol}" if symbol else ""
        return self._request("GET", f"/v2/openOrders{q}").get("orders") or []

    def get_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/v2/order?symbol={symbol}&client_order_id={client_order_id}")
        except StrikeAPIError as e:
            if e.status == 404:
                return None
            raise

    # ---- writes ---------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self._request("POST", "/v2/leverage", {"symbol": symbol, "leverage": leverage})

    def place_order(self, order: dict[str, Any], builder_fee_bps: int | None = None) -> dict[str, Any]:
        return self._request("POST", "/v2/order", order, builder_fee_bps=builder_fee_bps)

    def cancel_all(self, symbol: str | None = None) -> dict[str, Any]:
        return self._request("DELETE", "/v2/order/cancel-all", {"symbol": symbol} if symbol else {})
