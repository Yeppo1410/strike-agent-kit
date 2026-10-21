"""Read-only Hyperliquid funding feed (public info API, no keys).

Hyperliquid's `funding` in metaAndAssetCtxs is the hourly rate, same unit as
Strike's `funding_rate`, so the two compare directly.
"""

from __future__ import annotations

from decimal import Decimal as D

import httpx


class HyperliquidFunding:
    def __init__(self, info_url: str, http: httpx.Client | None = None):
        self.info_url = info_url
        self.http = http or httpx.Client(timeout=10.0)

    def get_funding(self) -> dict[str, dict[str, D]]:
        res = self.http.post(self.info_url, json={"type": "metaAndAssetCtxs"})
        res.raise_for_status()
        meta, ctxs = res.json()
        out: dict[str, dict[str, D]] = {}
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset.get("isDelisted"):
                continue
            out[asset["name"]] = {
                "funding_hourly": D(str(ctx.get("funding") or "0")),
                "mark": D(str(ctx.get("markPx") or "0")),
            }
        return out
