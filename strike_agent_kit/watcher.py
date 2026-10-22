"""Background watcher for protection rules, plus an optional desktop notification when one fires.

Runs as a daemon thread inside the MCP server (and the standalone dashboard). Every few seconds
it checks the armed rules against Strike's mark prices. It only calls Strike while at least one
rule is armed. Interval: STRIKE_WATCH_INTERVAL_S (default 5). Notifications: STRIKE_NOTIFY=0 turns
them off; they use macOS Notification Center and are skipped elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

from .service import AgentService


def notify_desktop(title: str, message: str) -> None:
    if sys.platform != "darwin" or os.environ.get("STRIKE_NOTIFY", "1") == "0":
        return
    # json.dumps gives a double-quoted string with backslash escapes, which AppleScript accepts.
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001  a notification is a nicety, never a failure
        pass


def _notify_rule(rule: dict[str, Any], out: dict[str, Any]) -> None:
    res = out.get("result") or {}
    headline = {"executed": "Protection rule fired", "noop": "Protection rule fired (nothing to do)",
                "blocked": "Protection rule fired, but was blocked", "failed": "Protection rule fired, order failed"}
    detail = rule["text"].replace("If ", "", 1) + f" (mark {out['mark']})"
    if out["outcome"] == "executed" and res.get("order"):
        o = res["order"]
        detail += f". {o['side'].capitalize()} {o['size']} {o['symbol'].replace('-USD', '')}"
        if res.get("avg_price"):
            detail += f" @ {res['avg_price']}"
    elif res.get("violations"):
        detail += ". " + " ".join(res["violations"])
    threading.Thread(target=notify_desktop, args=(headline.get(out["outcome"], "Protection rule"), detail),
                     daemon=True).start()


def start_watcher(service: AgentService, interval_s: float | None = None) -> threading.Thread:
    interval = interval_s if interval_s is not None else float(os.environ.get("STRIKE_WATCH_INTERVAL_S", "5"))
    if _notify_rule not in service.on_rule_fired:
        service.on_rule_fired.append(_notify_rule)

    def loop() -> None:
        while True:
            time.sleep(max(1.0, interval))
            try:
                with service.lock:
                    service.check_protection_rules()
                service.watcher_error = None
            except Exception as e:  # noqa: BLE001  network hiccups: keep watching, show the error
                service.watcher_error = str(e)

    t = threading.Thread(target=loop, name="rule-watcher", daemon=True)
    t.start()
    return t
