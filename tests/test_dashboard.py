import json
import threading
import urllib.error
import urllib.request

from strike_agent_kit.dashboard import serve
from tests.test_service import svc  # noqa: F401  (fixture)


def _start(service):
    httpd = serve(service, 0)  # port 0 = any free port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def _post(url, body, header=True):
    headers = {"Content-Type": "application/json"}
    if header:
        headers["X-Strike-Dashboard"] = "1"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    return json.load(urllib.request.urlopen(req))


def test_state_and_human_kill_switch(svc):  # noqa: F811
    base, httpd = _start(svc)
    try:
        svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)
        state = json.load(urllib.request.urlopen(base + "/api/state"))
        assert state["account"]["positions"][0]["symbol"] == "ADA-USD"
        assert "<title>Strike Agent Kit</title>" in urllib.request.urlopen(base + "/").read().decode()

        # cross-site style request without the custom header is refused
        try:
            _post(base + "/api/kill-switch", {"on": True}, header=False)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        assert svc.policies.load().kill_switch is False

        assert _post(base + "/api/kill-switch", {"on": True}) == {"kill_switch": True}
        assert svc.place_order("ADA", "sell", notional_usd=100, dry_run=False)["status"] == "blocked"
        assert _post(base + "/api/kill-switch", {"on": False}) == {"kill_switch": False}
        assert svc.place_order("ADA", "sell", notional_usd=100, dry_run=False)["status"] == "submitted"
        events = [e["event"] for e in svc.get_action_log(10)]
        assert "kill_switch_released" in events
    finally:
        httpd.shutdown()


def test_approve_and_reject_via_dashboard(svc):  # noqa: F811
    path = svc.settings.guardrails_path
    path.write_text(json.dumps({**json.loads(path.read_text()), "require_human_approval": True}))
    base, httpd = _start(svc)
    try:
        a1 = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)["approval_id"]
        a2 = svc.place_order("BTC", "buy", notional_usd=300, dry_run=False)["approval_id"]
        state = json.load(urllib.request.urlopen(base + "/api/state"))
        assert {a["id"] for a in state["approvals"] if a["status"] == "pending"} == {a1, a2}
        assert all("args" not in a for a in state["approvals"])

        try:  # no custom header -> refused, nothing executes
            _post(base + f"/api/approvals/{a1}", {"action": "approve"}, header=False)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        assert svc.backend.get_positions() == []

        assert _post(base + f"/api/approvals/{a1}", {"action": "approve"})["status"] == "executed"
        assert _post(base + f"/api/approvals/{a2}", {"action": "reject"})["status"] == "rejected"
        assert [p["symbol"] for p in svc.backend.get_positions()] == ["ADA-USD"]
    finally:
        httpd.shutdown()
