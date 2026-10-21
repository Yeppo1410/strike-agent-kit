"""Open the dashboard on a mock account with some agent activity already in it.

    python scripts/dashboard_preview.py      -> http://127.0.0.1:8787

Useful for screenshots and for checking the dashboard without an agent running.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

work = Path(tempfile.mkdtemp(prefix="strike-dash-"))
(work / "guardrails.json").write_text((ROOT / "guardrails.json").read_text())
os.environ.update({"STRIKE_MODE": "mock", "STRIKE_GUARDRAILS": str(work / "guardrails.json"),
                   "STRIKE_DATA_DIR": str(work / "data"), "STRIKE_ENV_FILE": str(work / ".env")})

from strike_agent_kit.dashboard import serve  # noqa: E402
from strike_agent_kit.factory import build_service  # noqa: E402

svc = build_service()
svc.get_account()  # record day-start equity
svc.hedge_exposure("ADA", 3000, reason="user holds 3,000 ADA spot", dry_run=True)
aid = svc.hedge_exposure("ADA", 3000, dry_run=False, reason="user asked to hedge 3,000 ADA")["approval_id"]
svc.decide_approval_by_human(aid, approve=True)
aid = svc.place_order("BTC", "buy", notional_usd=800, dry_run=False, reason="user: small BTC long, 5x max")["approval_id"]
svc.decide_approval_by_human(aid, approve=True)
svc.place_order("SOL", "sell", notional_usd=4000, leverage=10, dry_run=False,
                reason="capture SOL funding spread")
# left waiting, so the approval card shows on the dashboard
svc.place_order("ETH", "buy", notional_usd=600, dry_run=False, reason="user: add some ETH exposure")
svc.backend.move_price("ADA-USD", -4)
svc.backend.move_price("BTC-USD", 1.2)

port = int(os.environ.get("STRIKE_DASHBOARD_PORT", "8787"))
print(f"Dashboard preview on http://127.0.0.1:{port}  (mock data)")
serve(svc, port).serve_forever()
