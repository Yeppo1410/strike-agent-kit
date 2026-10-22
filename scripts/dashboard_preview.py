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
from strike_agent_kit.watcher import start_watcher  # noqa: E402

svc = build_service()
svc.get_account()  # record day-start equity
aid = svc.place_order("BTC", "buy", notional_usd=800, dry_run=False, reason="user: small BTC long, 5x max")["approval_id"]
svc.decide_approval_by_human(aid, approve=True)
svc.place_order("SOL", "sell", notional_usd=4000, leverage=10, dry_run=False,
                reason="capture SOL funding spread")
# An ADA protection rule, armed earlier, fires when ADA slips below $0.70 (shows the "fired" banner).
rule = svc.propose_protection_rule("ADA", "below", 0.70, spot_amount=3000,
                                   reason="user holds 3,000 ADA spot and wants cover below $0.70")
svc.decide_approval_by_human(rule["approval_id"], approve=True)
svc.backend.move_price("ADA-USD", -4)
svc.check_protection_rules()
# A stop-loss that is armed and being watched, and a new rule left waiting for approval.
stop = svc.propose_protection_rule("BTC", "below", 92000, action="close", reason="user: stop-loss on the BTC long")
svc.decide_approval_by_human(stop["approval_id"], approve=True)
svc.propose_protection_rule("ETH", "below", 3400, spot_amount=0.5, reason="user holds 0.5 ETH, cover below $3,400")
svc.backend.move_price("BTC-USD", 1.2)

port = int(os.environ.get("STRIKE_DASHBOARD_PORT", "8787"))
print(f"Dashboard preview on http://127.0.0.1:{port}  (mock data)")
start_watcher(svc)
serve(svc, port).serve_forever()
