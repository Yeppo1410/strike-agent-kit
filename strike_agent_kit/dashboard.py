"""Local control panel for the human: order approvals, account, guardrails, kill switch, agent activity.

Runs inside the MCP server process (so it sees the same state, also in mock mode) on
http://127.0.0.1:8787. Binds to localhost only. The kill switch can be released here and
nowhere else, and orders waiting for approval can only be approved here: there are no MCP tools
for either.

Standalone (e.g. against testnet while no agent is running):
    python -m strike_agent_kit.dashboard
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .service import AgentService

_FUNDING_TTL_S = 30


def _make_handler(service: AgentService):
    cache: dict[str, Any] = {"funding": None, "funding_at": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # stdout belongs to MCP stdio; stay quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            try:
                if self.path in ("/", "/index.html"):
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if self.path == "/api/state":
                    with service.lock:
                        data = {"account": service.get_account(), "guardrails": service.get_guardrails()["policy"],
                                "log": service.get_action_log(40), "approvals": service.list_approvals(50),
                                "ts": time.time()}
                    return self._json(200, data)
                if self.path == "/api/funding":
                    if time.time() - cache["funding_at"] > _FUNDING_TTL_S:
                        with service.lock:
                            cache["funding"] = service.get_funding_board()
                        cache["funding_at"] = time.time()
                    return self._json(200, cache["funding"])
                self._json(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})

        def do_POST(self) -> None:  # noqa: N802
            # A custom header forces a CORS preflight, which this server never approves,
            # so other web pages in your browser cannot flip the switch.
            if self.headers.get("X-Strike-Dashboard") != "1":
                return self._json(403, {"error": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/kill-switch":
                    with service.lock:
                        return self._json(200, service.set_kill_switch_by_human(bool(body.get("on"))))
                if self.path.startswith("/api/approvals/"):
                    approval_id = self.path.rsplit("/", 1)[-1]
                    action = body.get("action")
                    if action not in ("approve", "reject"):
                        return self._json(400, {"error": "action must be approve or reject"})
                    with service.lock:
                        return self._json(200, service.decide_approval_by_human(approval_id, action == "approve"))
                self._json(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                self._json(400, {"error": str(e)})

    return Handler


def serve(service: AgentService, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), _make_handler(service))


def start_dashboard_thread(service: AgentService) -> int | None:
    port = int(os.environ.get("STRIKE_DASHBOARD_PORT", "8787"))
    if port == 0:
        return None
    try:
        httpd = serve(service, port)
    except OSError:
        print(f"[strike-agent-kit] dashboard port {port} in use, dashboard disabled", file=sys.stderr)
        return None
    threading.Thread(target=httpd.serve_forever, name="dashboard", daemon=True).start()
    print(f"[strike-agent-kit] dashboard on http://127.0.0.1:{port}", file=sys.stderr)
    return port


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strike Agent Kit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Inter+Tight:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f4f2ee; --panel:#ffffff; --ink:#16151a; --muted:#6c6a72; --line:#e3e0da;
  --ok:#1d7a4f; --ok-bg:#e3f2ea; --warn:#9a6200; --warn-bg:#fbefd6; --bad:#b4262c; --bad-bg:#fbe3e3;
  --accent:#ff3d6e; --chip:#efece6;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg:#0f0f12; --panel:#17171c; --ink:#ecebef; --muted:#9a98a3; --line:#2a2a31;
  --ok:#4fd393; --ok-bg:#12291f; --warn:#f2b54a; --warn-bg:#2c2312; --bad:#ff6b70; --bad-bg:#2e1517;
  --accent:#ff5c87; --chip:#22222a;
}}
:root[data-theme="dark"]{
  --bg:#0f0f12; --panel:#17171c; --ink:#ecebef; --muted:#9a98a3; --line:#2a2a31;
  --ok:#4fd393; --ok-bg:#12291f; --warn:#f2b54a; --warn-bg:#2c2312; --bad:#ff6b70; --bad-bg:#2e1517;
  --accent:#ff5c87; --chip:#22222a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "Inter Tight",system-ui,sans-serif}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:20px;margin:0;font-weight:700;letter-spacing:-.01em}
h1 span{color:var(--accent)}
.badge{font:500 12px "IBM Plex Mono",monospace;padding:3px 8px;border-radius:99px;background:var(--chip);color:var(--muted);text-transform:uppercase}
.spacer{flex:1}
.ks{display:flex;align-items:center;gap:12px;padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:var(--panel)}
.ks.on{border-color:var(--bad);background:var(--bad-bg)}
.ks b{font-size:14px}.ks small{display:block;color:var(--muted);font-size:12px}
button{font:600 13px "Inter Tight",sans-serif;border:0;border-radius:9px;padding:9px 14px;cursor:pointer;background:var(--ink);color:var(--panel)}
.ks.on button{background:var(--ok);color:#fff}
.ks:not(.on) button{background:var(--bad);color:#fff}
.grid{display:grid;gap:14px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0}
.card h2{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:0 0 12px}
.stat{grid-column:span 3}.half{grid-column:span 6}.wide{grid-column:span 7}.side{grid-column:span 5}.full{grid-column:span 12}
.big{font:600 26px "IBM Plex Mono",monospace;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.bar{height:6px;border-radius:9px;background:var(--chip);margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ok);width:0}
.bar.warn i{background:var(--warn)}.bar.bad i{background:var(--bad)}
.pos{color:var(--ok)}.neg{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right}
.scroll{overflow-x:auto}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.rules{display:grid;grid-template-columns:1fr auto;gap:6px 12px;font-size:13px}
.rules span:nth-child(odd){color:var(--muted)}
.feed{display:flex;flex-direction:column;gap:8px;max-height:520px;overflow:auto}
.ev{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;padding:10px;border-radius:10px;background:var(--bg)}
.chip{font:500 11px "IBM Plex Mono",monospace;padding:2px 7px;border-radius:6px;height:fit-content;white-space:nowrap}
.c-ok{background:var(--ok-bg);color:var(--ok)}.c-warn{background:var(--warn-bg);color:var(--warn)}
.c-bad{background:var(--bad-bg);color:var(--bad)}.c-n{background:var(--chip);color:var(--muted)}
.ev .t{font-size:13px}.ev .m{grid-column:2;color:var(--muted);font-size:12px}
.ev .why{grid-column:2;font-size:12px;font-style:italic;color:var(--muted)}
.approvals{display:flex;flex-direction:column;gap:12px;margin-bottom:14px}
.ap{display:grid;grid-template-columns:1fr auto;gap:10px 18px;align-items:center;padding:16px;border-radius:14px;
  border:1px solid var(--warn);background:var(--warn-bg)}
.ap h3{margin:0;font-size:17px;letter-spacing:-.01em}
.ap .meta{color:var(--muted);font-size:13px;margin-top:4px}
.ap .why{font-style:italic;font-size:13px;margin-top:6px}
.ap .btns{display:flex;gap:8px}
.ap .ok{background:var(--ok);color:#fff}.ap .no{background:transparent;color:var(--ink);border:1px solid var(--line)}
.ap .tag{margin-bottom:6px;display:block;font:500 11px "IBM Plex Mono",monospace;color:var(--warn);text-transform:uppercase;letter-spacing:.06em}
@media (max-width:860px){.stat{grid-column:span 6}.half,.wide,.side{grid-column:span 12}}
@media (max-width:480px){.stat{grid-column:span 12}.ap{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Strike <span>Agent Kit</span></h1>
    <span class="badge" id="mode">…</span>
    <div class="spacer"></div>
    <div class="ks" id="ks">
      <div><b id="ksTitle">Kill switch off</b><small id="ksSub">Agent may open new positions within limits</small></div>
      <button id="ksBtn">Engage</button>
    </div>
  </header>

  <div class="approvals" id="approvals"></div>

  <div class="grid">
    <div class="card stat"><h2>Equity</h2><div class="big mono" id="equity">–</div><div class="sub" id="avail">–</div></div>
    <div class="card stat"><h2>Today</h2><div class="big mono" id="day">–</div>
      <div class="sub" id="dayLimit">–</div><div class="bar" id="dayBar"><i></i></div></div>
    <div class="card stat"><h2>Exposure</h2><div class="big mono" id="exp">–</div>
      <div class="sub" id="expLimit">–</div><div class="bar" id="expBar"><i></i></div></div>
    <div class="card stat"><h2>Unrealized PnL</h2><div class="big mono" id="upnl">–</div><div class="sub" id="npos">–</div></div>

    <div class="card wide"><h2>Positions</h2><div class="scroll"><table>
      <thead><tr><th>Market</th><th>Side</th><th class="r">Size</th><th class="r">Entry</th><th class="r">Mark</th><th class="r">Notional</th><th class="r">PnL</th><th class="r">Lev</th></tr></thead>
      <tbody id="positions"></tbody></table></div></div>

    <div class="card side"><h2>Guardrails</h2><div class="rules" id="rules"></div></div>

    <div class="card half"><h2>Agent activity</h2><div class="feed" id="feed"></div></div>

    <div class="card half"><h2>Funding: Strike vs Hyperliquid (hourly)</h2><div class="scroll"><table>
      <thead><tr><th>Market</th><th class="r">Strike APR</th><th class="r">HL APR</th><th class="r">Spread APR</th><th>Trade</th><th class="r">Breakeven</th></tr></thead>
      <tbody id="funding"></tbody></table></div>
      <div class="sub">Refreshes every 30 s. Breakeven = hours the spread must last to cover taker fees on both venues.</div></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const usd = (n, sign) => (sign && n > 0 ? "+" : "") + (n < 0 ? "−$" : "$") + Math.abs(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let killOn = false;

function setBar(el, frac){ const f = Math.max(0, Math.min(1, frac)); el.querySelector("i").style.width = (f*100)+"%";
  el.className = "bar" + (f >= 1 ? " bad" : f >= .7 ? " warn" : ""); }

const EVENTS = {
  order_submitted: ["filled", "c-ok"], order_blocked: ["blocked", "c-bad"], order_preview: ["preview", "c-n"],
  order_needs_confirmation: ["needs ok", "c-warn"], order_pending_approval: ["awaiting you", "c-warn"],
  order_rejected_by_human: ["you rejected", "c-n"], order_invalid: ["invalid", "c-n"], order_error: ["error", "c-bad"],
  kill_switch_engaged: ["kill switch on", "c-bad"], kill_switch_released: ["kill switch off", "c-ok"], cancel_all: ["cancel all", "c-n"],
};

function describe(e){
  const o = e.order || {};
  if (o.symbol){
    const n = e.decision?.order_notional_usd ?? e.guardrails?.order_notional_usd;
    let t = `${o.side === "buy" ? "Buy" : "Sell"} ${o.size} ${o.symbol.replace("-USD","")}` + (n ? ` (${usd(n)})` : "") + (o.reduce_only ? " · reduce-only" : "");
    if (e.event === "order_submitted") t += ` · ${e.exchange_status}` + (e.avg_price ? ` @ ${e.avg_price}` : "");
    return t;
  }
  if (e.event === "order_invalid") return e.error;
  if (e.event === "cancel_all") return "Cancel open orders" + (e.symbol ? " on " + e.symbol : "");
  return "";
}

async function state(){
  const r = await fetch("/api/state"); if (!r.ok) return; const s = await r.json();
  const a = s.account, g = s.guardrails;
  $("mode").textContent = a.mode;
  killOn = a.kill_switch;
  $("ks").classList.toggle("on", killOn);
  $("ksTitle").textContent = killOn ? "Kill switch ON" : "Kill switch off";
  $("ksSub").textContent = killOn ? "New risk blocked. Closing still works." : "Agent may open new positions within limits";
  $("ksBtn").textContent = killOn ? "Release" : "Engage";

  $("equity").textContent = usd(a.equity_usd); $("avail").textContent = usd(a.available_usd) + " available";
  $("day").textContent = usd(a.day_pnl_usd, true); $("day").className = "big mono " + (a.day_pnl_usd < 0 ? "neg" : a.day_pnl_usd > 0 ? "pos" : "");
  $("dayLimit").textContent = "Loss limit " + usd(-a.daily_loss_limit_usd);
  setBar($("dayBar"), a.day_pnl_usd < 0 ? -a.day_pnl_usd / a.daily_loss_limit_usd : 0);
  const exp = a.positions.reduce((t,p) => t + p.notional_usd, 0);
  $("exp").textContent = usd(exp); $("expLimit").textContent = "Cap " + usd(g.max_total_notional_usd);
  setBar($("expBar"), exp / g.max_total_notional_usd);
  $("upnl").textContent = usd(a.unrealized_pnl_usd, true); $("upnl").className = "big mono " + (a.unrealized_pnl_usd < 0 ? "neg" : a.unrealized_pnl_usd > 0 ? "pos" : "");
  $("npos").textContent = a.positions.length + " position" + (a.positions.length === 1 ? "" : "s") + " · " + a.open_orders + " open orders";

  $("positions").innerHTML = a.positions.length ? a.positions.map(p => `<tr>
    <td><b>${esc(p.symbol.replace("-USD",""))}</b></td><td class="${p.side === "long" ? "pos" : "neg"}">${p.side}</td>
    <td class="r mono">${esc(p.size)}</td><td class="r mono">${(+p.entry_price).toPrecision(6)}</td><td class="r mono">${(+p.mark_price).toPrecision(6)}</td>
    <td class="r mono">${usd(p.notional_usd)}</td><td class="r mono ${p.unrealized_pnl_usd < 0 ? "neg" : "pos"}">${usd(p.unrealized_pnl_usd, true)}</td>
    <td class="r mono">${esc(p.leverage)}x</td></tr>`).join("") : `<tr><td colspan="8" class="empty">No open positions</td></tr>`;

  $("rules").innerHTML = [
    ["Max leverage", g.max_leverage + "x"], ["Max per order", usd(g.max_order_notional_usd)],
    ["Max per market", usd(g.max_position_notional_usd)], ["Max total exposure", usd(g.max_total_notional_usd)],
    ["Daily loss limit", usd(g.daily_loss_limit_usd)],
    ["Your approval", g.require_human_approval ? (g.auto_approve_below_usd > 0 ? "above " + usd(g.auto_approve_below_usd) : "every new trade") : "off (ask in chat above " + usd(g.confirm_above_usd) + ")"],
    ["Max slippage", (g.max_slippage*100).toFixed(2) + "%"], ["Close slippage", (g.close_max_slippage*100).toFixed(2) + "%"], ["Markets", g.allowed_markets.length ? g.allowed_markets.map(m => m.replace("-USD","")).join(", ") : "all"],
  ].map(([k,v]) => `<span>${k}</span><span class="mono">${esc(v)}</span>`).join("");

  const pending = (s.approvals || []).filter(a => a.status === "pending");
  $("approvals").innerHTML = pending.map(a => {
    const o = a.order, mins = Math.max(0, Math.round((a.expires - s.ts) / 60));
    return `<div class="ap"><div>
      <div class="tag">Agent wants to trade · waiting for you · expires in ${mins} min</div>
      <h3>${o.side === "buy" ? "Buy" : "Sell"} ${esc(o.size)} ${esc(o.symbol.replace("-USD",""))} · ${usd(a.notional_usd)}</h3>
      <div class="meta mono">${esc(o.type)}${o.price ? " @ " + esc(o.price) : " ~" + esc(o.est_price)}${o.leverage ? " · " + esc(o.leverage) + "x" : ""}${o.reduce_only ? " · reduce-only" : ""}</div>
      ${a.reason ? `<div class="why">Agent's reason: “${esc(a.reason)}”</div>` : ""}
      ${o.note ? `<div class="meta">${esc(o.note)}</div>` : ""}
    </div><div class="btns"><button class="no" data-id="${a.id}" data-act="reject">Reject</button>
      <button class="ok" data-id="${a.id}" data-act="approve">Approve</button></div></div>`;
  }).join("");
  document.title = pending.length ? `(${pending.length}) Approve trade · Strike Agent Kit` : "Strike Agent Kit";

  const apStatus = Object.fromEntries((s.approvals || []).map(a => [a.id, a.status]));
  const RESOLVED = {executed: ["you approved", "c-ok"], rejected: ["you rejected", "c-n"], expired: ["expired", "c-n"],
                    blocked_on_approval: ["approved, then blocked", "c-bad"], failed: ["approved, failed", "c-bad"]};
  const log = s.log.slice().reverse();
  $("feed").innerHTML = log.length ? log.map(e => {
    let [label, cls] = EVENTS[e.event] || [e.event, "c-n"];
    if (e.event === "order_pending_approval" && RESOLVED[apStatus[e.approval_id]]) [label, cls] = RESOLVED[apStatus[e.approval_id]];
    const viol = (e.decision?.violations || e.guardrails?.violations || []).map(v => v.message).join(" ");
    const time = new Date(e.ts).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
    return `<div class="ev"><span class="chip ${cls}">${label}</span><span class="t">${esc(describe(e))} <span class="mono" style="color:var(--muted);font-size:11px">${time}</span></span>
      ${viol ? `<span class="m">${esc(viol)}</span>` : ""}${e.reason ? `<span class="why">“${esc(e.reason)}”</span>` : ""}</div>`;
  }).join("") : `<div class="empty">No agent activity yet</div>`;
}

async function funding(){
  const r = await fetch("/api/funding"); if (!r.ok) return; const f = await r.json();
  const tradeLabel = t => !t ? "–" : t.startsWith("short Strike") ? "Short Strike · long HL" : t.startsWith("long Strike") ? "Long Strike · short HL" : t;
  $("funding").innerHTML = f.rows.map(x => `<tr><td><b>${esc(x.symbol.replace("-USD",""))}</b></td>
    <td class="r mono">${x.strike_apr_pct.toFixed(1)}%</td><td class="r mono">${x.hyperliquid_apr_pct != null ? x.hyperliquid_apr_pct.toFixed(1) + "%" : "–"}</td>
    <td class="r mono"><b>${x.spread_apr_pct != null ? x.spread_apr_pct.toFixed(1) + "%" : "–"}</b></td>
    <td>${esc(tradeLabel(x.trade))}</td><td class="r mono">${x.breakeven_hours != null ? x.breakeven_hours + " h" : "–"}</td></tr>`).join("");
}

$("approvals").onclick = async ev => {
  const b = ev.target.closest("button[data-id]"); if (!b) return;
  b.disabled = true;
  await fetch(`/api/approvals/${b.dataset.id}`, {method:"POST", headers:{"Content-Type":"application/json","X-Strike-Dashboard":"1"},
    body: JSON.stringify({action: b.dataset.act})});
  state();
};
$("ksBtn").onclick = async () => {
  if (killOn && !confirm("Release the kill switch? The agent will be able to open new positions again.")) return;
  await fetch("/api/kill-switch", {method:"POST", headers:{"Content-Type":"application/json","X-Strike-Dashboard":"1"}, body: JSON.stringify({on: !killOn})});
  state();
};
state(); funding(); setInterval(state, 2000); setInterval(funding, 30000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    from .factory import build_service

    port = int(os.environ.get("STRIKE_DASHBOARD_PORT", "8787")) or 8787
    print(f"Dashboard on http://127.0.0.1:{port}  (mode: {os.environ.get('STRIKE_MODE', 'mock')})")
    serve(build_service(), port).serve_forever()
