import json
import time

from strike_agent_kit.watcher import start_watcher
from tests.test_service import svc  # noqa: F401  (fixture)


def _approval_mode(svc):  # noqa: F811
    path = svc.settings.guardrails_path
    path.write_text(json.dumps({**json.loads(path.read_text()), "require_human_approval": True}))


def _arm(svc, **kw):  # noqa: F811
    req = svc.propose_protection_rule(**kw)
    assert req["status"] == "pending_approval", req
    return req, svc.decide_approval_by_human(req["approval_id"], True)


def test_rule_waits_for_human_and_fires_once(svc):  # noqa: F811
    _approval_mode(svc)
    req = svc.propose_protection_rule("ADA", "below", 0.65, spot_amount=3000, reason="user asked")
    assert req["fires_immediately_on_approval"] is False
    assert req["distance_to_trigger_pct"] < 0
    assert req["preview_at_current_price"]["order"]["size"] == "3000"
    # proposing arms nothing: a price drop now does nothing
    svc.backend.move_price("ADA-USD", -20)
    assert svc.check_protection_rules() == []
    assert svc.backend.get_positions() == []
    svc.backend.move_price("ADA-USD", 25)  # back to 0.72

    res = svc.decide_approval_by_human(req["approval_id"], True)
    assert res["status"] == "armed" and res["fired"] is None
    rules = svc.list_protection_rules()
    assert rules[0]["status"] == "armed" and rules[0]["mark_now"]

    svc.backend.move_price("ADA-USD", -5)  # 0.684: above trigger
    assert svc.check_protection_rules() == []
    svc.backend.move_price("ADA-USD", -10)  # ~0.6156: below trigger
    fired = svc.check_protection_rules()
    assert fired[0]["outcome"] == "executed"
    # no second approval was needed, even though approval mode is on
    pos = svc.backend.get_positions()[0]
    assert pos["symbol"] == "ADA-USD" and pos["size"] == "-3000"
    assert svc.list_protection_rules()[0]["status"] == "executed"
    # one-shot: further drops do nothing
    svc.backend.move_price("ADA-USD", -10)
    assert svc.check_protection_rules() == []
    events = [e["event"] for e in svc.get_action_log(20)]
    assert {"protection_rule_proposed", "protection_rule_armed", "protection_rule_triggered",
            "protection_rule_executed"} <= set(events)


def test_rejected_rule_never_fires(svc):  # noqa: F811
    req = svc.propose_protection_rule("ADA", "below", 0.80, spot_amount=1000)
    assert req["fires_immediately_on_approval"] is True
    assert svc.decide_approval_by_human(req["approval_id"], False)["status"] == "rejected"
    assert svc.check_protection_rules() == []
    assert svc.backend.get_positions() == []
    assert svc.list_protection_rules()[0]["status"] == "rejected"


def test_condition_already_met_fires_on_arming(svc):  # noqa: F811
    _, res = _arm(svc, asset="ADA", condition="below", trigger_price=0.80, spot_amount=1000)
    assert res["fired"][0]["outcome"] == "executed"
    assert svc.backend.get_positions()[0]["size"] == "-1000"


def test_guardrails_still_apply_when_rule_fires(svc):  # noqa: F811
    _arm(svc, asset="ADA", condition="below", trigger_price=0.70, spot_amount=1000)
    svc.engage_kill_switch("user stopped trading")
    svc.backend.move_price("ADA-USD", -5)
    fired = svc.check_protection_rules()
    assert fired[0]["outcome"] == "blocked" and fired[0]["result"]["violations"]
    assert svc.backend.get_positions() == []
    # oversized hedge is blocked by the order cap too
    svc.set_kill_switch_by_human(False)
    _, res = _arm(svc, asset="ADA", condition="below", trigger_price=0.70, spot_amount=100000)
    assert res["fired"][0]["outcome"] == "blocked"  # price is already below 0.70: fires on arming
    assert svc.backend.get_positions() == []


def test_close_rule_and_above_condition(svc):  # noqa: F811
    svc.place_order("SOL", "buy", notional_usd=500, dry_run=False)
    _arm(svc, asset="SOL", condition="above", trigger_price=190, action="close", reason="take profit")
    svc.backend.move_price("SOL-USD", 6)  # 190.8
    assert svc.check_protection_rules()[0]["outcome"] == "executed"
    assert svc.backend.get_positions() == []


def test_cancel_and_validation(svc):  # noqa: F811
    req, _ = _arm(svc, asset="ADA", condition="below", trigger_price=0.5, spot_amount=1000)
    assert svc.cancel_protection_rule(req["rule_id"])["status"] == "cancelled"
    svc.backend.move_price("ADA-USD", -50)
    assert svc.check_protection_rules() == []
    assert svc.propose_protection_rule("ADA", "sideways", 0.5, spot_amount=1)["status"] == "invalid"
    assert svc.propose_protection_rule("ADA", "below", 0.5)["status"] == "invalid"  # no spot_amount
    assert svc.propose_protection_rule("ETH", "below", 3000, action="close")["status"] == "invalid"  # no position
    assert svc.propose_protection_rule("XYZ", "below", 1, spot_amount=1)["status"] == "invalid"
    assert svc.propose_protection_rule("ADA", "below", 0.5, spot_amount=1, expires_in_hours=500)["status"] == "invalid"
    for _ in range(svc.MAX_ACTIVE_RULES):
        assert svc.propose_protection_rule("ADA", "below", 0.1, spot_amount=10)["status"] == "pending_approval"
    assert "active rules" in svc.propose_protection_rule("ADA", "below", 0.1, spot_amount=10)["error"]


def test_armed_rule_expires(svc):  # noqa: F811
    req, _ = _arm(svc, asset="ADA", condition="below", trigger_price=0.5, spot_amount=1000, expires_in_hours=1)
    svc.journal.update_rule(req["rule_id"], expires=time.time() - 1)
    svc.backend.move_price("ADA-USD", -50)
    assert svc.check_protection_rules() == []
    assert svc.list_protection_rules()[0]["status"] == "expired"


def test_watcher_thread_fires_and_notifies(svc):  # noqa: F811
    seen = []
    svc.on_rule_fired.append(lambda rule, out: seen.append(out["outcome"]))
    _arm(svc, asset="ADA", condition="below", trigger_price=0.70, spot_amount=500)
    start_watcher(svc, interval_s=1)
    svc.backend.move_price("ADA-USD", -5)
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.1)
    assert seen == ["executed"]
    assert svc.get_order_status(svc.list_protection_rules()[0]["approval_id"])["rule_status"] == "executed"


def test_proposal_warns_when_guardrails_would_block(svc):  # noqa: F811
    req = svc.propose_protection_rule("ETH", "below", 3400, spot_amount=2)  # 2 ETH = $7,200 > $2,500 cap
    assert req["preview_at_current_price"]["status"] == "blocked"
    assert req["message"].startswith("At today's price the guardrails would block")
    card = next(a for a in svc.list_approvals() if a["id"] == req["approval_id"])
    assert card["preview_violations"]
