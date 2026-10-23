"""Agent-facing (MCP) order tools only ever create requests; the human executes them in the dashboard."""
import json

from tests.test_service import svc  # noqa: F401  (fixture)


def test_request_never_executes_even_with_approval_off(svc):  # noqa: F811
    # the fixture switches require_human_approval off; set auto-approve high too
    path = svc.settings.guardrails_path
    path.write_text(json.dumps({**json.loads(path.read_text()), "auto_approve_below_usd": 100000}))
    res = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False, _request_only=True)
    assert res["status"] == "pending_approval" and "Nothing has been traded" in res["message"]
    assert svc.backend.get_positions() == []
    hedge = svc.hedge_exposure("ADA", 1000, dry_run=False, _request_only=True)
    assert hedge["status"] == "pending_approval"
    assert svc.backend.get_positions() == []
    assert svc.decide_approval_by_human(res["approval_id"], True)["status"] == "executed"
    assert svc.backend.get_positions()[0]["size"] == "-694"


def test_preview_still_previews(svc):  # noqa: F811
    res = svc.place_order("ADA", "sell", notional_usd=500, dry_run=True, _request_only=True)
    assert res["status"] == "preview" and res["confirmation_token"] is None


def test_blocked_requests_never_reach_the_human(svc):  # noqa: F811
    res = svc.place_order("BTC", "sell", notional_usd=5000, leverage=10, dry_run=False, _request_only=True)
    assert res["status"] == "blocked"
    assert all(a["status"] != "pending" for a in svc.list_approvals())


def test_close_request_executes_on_click_even_with_kill_switch(svc):  # noqa: F811
    svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)
    svc.engage_kill_switch("stop")
    req = svc.close_position("ADA", _request_only=True)
    assert req["status"] == "pending_approval"
    assert svc.backend.get_positions()[0]["size"] == "-694"  # nothing closed yet
    svc.backend.move_price("ADA-USD", -10)
    out = svc.decide_approval_by_human(req["approval_id"], True)
    assert out["status"] == "executed"
    assert svc.backend.get_positions() == []


def test_close_request_retries_on_thin_book_when_executed(svc):  # noqa: F811
    from decimal import Decimal as D
    svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)
    svc.backend.book_gap["ADA-USD"] = D("0.02")  # beyond 0.5%, within the 3% close limit
    req = svc.close_position("ADA", _request_only=True)
    out = svc.decide_approval_by_human(req["approval_id"], True)
    assert out["status"] == "executed" and out["result"].get("retried_with_slippage") == "0.03"


def test_data_folder_moved_away_while_running(svc, tmp_path):  # noqa: F811
    import shutil
    data = tmp_path / "data"
    svc.journal.journal_path, svc.journal.state_path = data / "journal.jsonl", data / "state.json"
    data.mkdir()
    svc.get_account()
    shutil.move(str(data), str(tmp_path / "moved-away"))
    svc.get_account()  # recreates the folder instead of failing
    assert svc.place_order("ADA", "sell", notional_usd=100, dry_run=True)["status"] == "preview"
    assert (data / "journal.jsonl").is_file()
