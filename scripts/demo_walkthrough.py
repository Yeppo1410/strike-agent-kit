"""End-to-end rehearsal of the demo, over a real MCP stdio connection.

Starts the MCP server as a subprocess (mock mode unless STRIKE_MODE says otherwise),
then calls the tools in the same order as the demo video.

    python scripts/demo_walkthrough.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def show(title: str, result) -> dict:
    data = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if data is None:
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"text": text}
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    print(f"\n=== {title} ===")
    print(json.dumps(data, indent=2)[:2500])
    return data


async def main() -> None:
    port = os.environ.get("STRIKE_DASHBOARD_PORT", "8799")
    work = Path(tempfile.mkdtemp(prefix="strike-demo-"))
    (work / "guardrails.json").write_text((ROOT / "guardrails.json").read_text())
    env = {**os.environ, "STRIKE_MODE": os.environ.get("STRIKE_MODE", "mock"),
           "STRIKE_GUARDRAILS": str(work / "guardrails.json"), "STRIKE_DATA_DIR": str(work / "data"),
           "STRIKE_ENV_FILE": str(work / ".env"), "PYTHONPATH": str(ROOT), "STRIKE_DASHBOARD_PORT": port,
           # USD margin by default, like Strike; set STRIKE_COLLATERAL_ASSET=ADA to rehearse ADA-held margin.
           "STRIKE_COLLATERAL_ASSET": os.environ.get("STRIKE_COLLATERAL_ASSET", "")}
    mock = env["STRIKE_MODE"] == "mock"

    def human_clicks(approval_id: str, action: str) -> None:
        """Stands in for you clicking Approve/Reject in the dashboard."""
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/approvals/{approval_id}",
                                     data=json.dumps({"action": action}).encode(),
                                     headers={"Content-Type": "application/json", "X-Strike-Dashboard": "1"})
        print(f"\n>>> human clicks {action.upper()} in the dashboard:", json.load(urllib.request.urlopen(req))["status"])
    params = StdioServerParameters(command=sys.executable, args=["-m", "strike_agent_kit.server"], env=env,
                                   cwd=str(work))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("Tools:", ", ".join(t.name for t in tools.tools))

            show("1. Funding board: where is the edge?", await s.call_tool("get_funding_board", {}))
            show("2. Account before", await s.call_tool("get_account", {}))

            show("3. Hedge 3,000 ADA (preview)", await s.call_tool(
                "hedge_exposure", {"asset": "ADA", "spot_amount": 3000, "reason": "user holds 3,000 ADA spot"}))
            req = show("4. User says go -> order waits for approval in the dashboard", await s.call_tool(
                "hedge_exposure", {"asset": "ADA", "spot_amount": 3000, "dry_run": False,
                                   "reason": "user asked to hedge 3,000 ADA"}))
            human_clicks(req["approval_id"], "approve")
            show("4b. Agent checks the outcome", await s.call_tool(
                "get_order_status", {"approval_id": req["approval_id"]}))

            casual = show("4c. Casual remark mis-read as a trade -> it only waits; human rejects", await s.call_tool(
                "place_order", {"symbol": "BTC", "side": "buy", "notional_usd": 300, "dry_run": False,
                                "reason": "user said BTC looks strong"}))
            human_clicks(casual["approval_id"], "reject")

            show("5. Agent tries to go big: 10x leverage, $4,000 SOL short", await s.call_tool(
                "place_order", {"symbol": "SOL", "side": "sell", "notional_usd": 4000, "leverage": 10,
                                "dry_run": False, "reason": "capture SOL funding spread"}))

            rule = show("5a. 'If ADA drops below $0.65, cover all 5,000 of my ADA' -> rule waits for the human",
                        await s.call_tool("propose_protection_rule", {
                            "asset": "ADA", "condition": "below", "trigger_price": 0.65, "spot_amount": 5000,
                            "reason": "user holds 5,000 ADA and wants full cover below $0.65"}))
            human_clicks(rule["approval_id"], "approve")
            if mock:
                show("5a2. Market moves: ADA -10% (simulated) -> the rule fires by itself", await s.call_tool(
                    "simulate_price_move", {"asset": "ADA", "pct": -10}))
            show("5a3. Rules", await s.call_tool("list_protection_rules", {}))
            if not mock:  # don't leave a live rule armed after a rehearsal
                show("5a4. Cancel the rule", await s.call_tool("cancel_protection_rule", {"rule_id": rule["rule_id"]}))

            if mock:  # a real 25% drop can't be ordered up on testnet/mainnet
                show("5b. What if ADA drops another 25%? (5,000 ADA spot, now fully hedged)", await s.call_tool(
                    "what_if", {"moves": {"ADA": -25}, "spot": {"ADA": 5000}}))
                show("5c. Market moves: ADA -25% (simulated)", await s.call_tool(
                    "simulate_price_move", {"asset": "ADA", "pct": -25}))
                show("5d. The short gains what the spot loses", await s.call_tool("get_account", {}))

            loosen = show("5f. Agent asks to raise the max order size -> waits for the human", await s.call_tool(
                "propose_guardrail_change", {"changes": {"max_order_notional_usd": 6000},
                                             "reason": "user asked to raise max order to $6,000"}))
            human_clicks(loosen["approval_id"], "reject")
            show("5g. Agent tightens the daily loss limit -> applies at once", await s.call_tool(
                "propose_guardrail_change", {"changes": {"daily_loss_limit_usd": 100},
                                             "reason": "user asked to lower the daily loss limit"}))
            show("5h. Limits now in force", await s.call_tool("get_guardrails", {}))

            show("6. Engage kill switch", await s.call_tool(
                "engage_kill_switch", {"reason": "demo: user wants to stop new risk"}))
            show("7. New risk is blocked", await s.call_tool(
                "place_order", {"symbol": "BTC", "side": "buy", "notional_usd": 200, "dry_run": False}))
            show("8. Closing still works", await s.call_tool("close_position", {"symbol": "ADA"}))
            show("9. Audit log", await s.call_tool("get_action_log", {"limit": 10}))


if __name__ == "__main__":
    asyncio.run(main())
