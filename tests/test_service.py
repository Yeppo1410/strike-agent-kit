import json
from decimal import Decimal as D

import pytest

from strike_agent_kit.config import Settings
from strike_agent_kit.guardrails import DEFAULT_POLICY, PolicyStore
from strike_agent_kit.journal import Journal
from strike_agent_kit.mock import MockHyperliquid, MockStrike
from strike_agent_kit.service import AgentService, round_to_grid


@pytest.fixture
def svc(tmp_path):
    settings = Settings(mode="mock", base_url="", api_public_key=None, api_private_key=None,
                        builder_fee_bps=10, guardrails_path=tmp_path / "guardrails.json",
                        journal_path=tmp_path / "j.jsonl", state_path=tmp_path / "s.json",
                        hyperliquid_url="", use_hyperliquid=True)
    # Most tests exercise the pipeline directly; approval-mode tests switch it on themselves.
    (tmp_path / "guardrails.json").write_text(json.dumps({**DEFAULT_POLICY, "require_human_approval": False}))
    return AgentService(settings, MockStrike(), MockHyperliquid(), PolicyStore(settings.guardrails_path),
                        Journal(settings.journal_path, settings.state_path), poll_timeout_s=0)


def test_round_to_grid():
    assert round_to_grid(D("1234.9"), D("1"), D("1")) == D("1234")
    assert round_to_grid(D("0.123456"), D("0.00001"), D("0.00001")) == D("0.12345")
    assert round_to_grid(D("0.72349"), D("0.01"), D("0.0001")) == D("0.7234")


def test_funding_board_ranks_by_spread(svc):
    rows = svc.get_funding_board()["rows"]
    assert rows[0]["symbol"] == "ADA-USD"
    assert rows[0]["trade"] == "short Strike / long Hyperliquid"
    assert rows[0]["breakeven_hours"] > 0
    assert svc.get_funding_board(["sol"])["rows"][0]["symbol"] == "SOL-USD"


def test_funding_board_handles_live_market_shapes(svc):
    # Shapes seen on mainnet 21 Oct: a pre_trading listing with mark 0, and stock perps Hyperliquid doesn't list.
    from strike_agent_kit.mock import _market
    svc.backend.markets["DOGE-USD"] = {**_market("DOGE-USD", "DOGE", "0", "0", "0.00001", "1", "0.00001", 10),
                                       "status": "pre_trading"}
    svc.backend.markets["TSLA-USD"] = _market("TSLA-USD", "TSLA", "386.6", "0", "0.01", "0.01", "0.01", 10)
    rows = {r["symbol"]: r for r in svc.get_funding_board()["rows"]}
    assert "DOGE-USD" not in rows
    assert "spread_hourly" not in rows["TSLA-USD"]  # no Hyperliquid pair: Strike side only
    assert svc.get_funding_board(["doge"])["rows"][0]["symbol"] == "DOGE-USD"
    with pytest.raises(Exception, match="not open for trading"):
        svc._market("DOGE-USD")


def test_dry_run_sends_nothing(svc):
    res = svc.place_order("ADA", "sell", notional_usd=500, dry_run=True)
    assert res["status"] == "preview"
    assert svc.backend.get_positions() == []


def test_small_order_fills_with_builder_fee(svc):
    res = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False, reason="test")
    assert res["status"] == "submitted" and res["exchange_status"] == "filled"
    assert res["order"]["size"] == "694"  # 500 / 0.72 rounded down to the 1 ADA step
    assert svc.backend.builder_fees_paid > 0
    assert svc.get_account()["positions"][0]["side"] == "short"


def test_confirmation_flow(svc):
    first = svc.place_order("ADA", "sell", notional_usd=1500, dry_run=False)
    assert first["status"] == "needs_confirmation" and first["confirmation_token"]
    assert svc.backend.get_positions() == []
    bad = svc.place_order("ADA", "sell", notional_usd=1500, dry_run=False, confirmation_token="nope")
    assert bad["status"] == "needs_confirmation"
    ok = svc.place_order("ADA", "sell", notional_usd=1500, dry_run=False,
                         confirmation_token=bad["confirmation_token"])
    assert ok["status"] == "submitted"


def test_token_is_bound_to_the_order(svc):
    tok = svc.place_order("ADA", "sell", notional_usd=1500, dry_run=True)["confirmation_token"]
    other = svc.place_order("ADA", "sell", notional_usd=2000, dry_run=False, confirmation_token=tok)
    assert other["status"] == "needs_confirmation"


def test_blocked_order_is_logged(svc):
    res = svc.place_order("ADA", "sell", notional_usd=3000, dry_run=False)
    assert res["status"] == "blocked"
    assert any(v["rule"] == "max_order_notional" for v in res["guardrails"]["violations"])
    assert svc.get_action_log(1)[0]["event"] == "order_blocked"


def test_hedge_then_rehedge_trades_only_the_difference(svc):
    first = svc.hedge_exposure("ADA", 1000, dry_run=False)
    assert first["status"] == "submitted"
    assert svc.backend.get_positions()[0]["size"] == "-1000"
    second = svc.hedge_exposure("ADA", 1300, dry_run=False)
    assert second["order"]["size"] == "300" and second["order"]["side"] == "sell"
    third = svc.hedge_exposure("ADA", 1300, dry_run=False)
    assert third["status"] == "noop"
    fourth = svc.hedge_exposure("ADA", 1300, hedge_ratio=0.5, dry_run=False)
    assert fourth["order"]["side"] == "buy" and fourth["order"]["reduce_only"] is True
    # sizes sent to the exchange must be plain decimals, never scientific notation
    assert all("E" not in o["Size"] for o in svc.backend.orders.values())


def test_kill_switch_blocks_new_risk_but_close_works(svc):
    svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)
    svc.engage_kill_switch("test")
    assert svc.place_order("ADA", "sell", notional_usd=100, dry_run=False)["status"] == "blocked"
    closed = svc.close_position("ADA")
    assert closed["status"] == "submitted"
    assert svc.backend.get_positions() == []


def test_daily_loss_limit_trips_after_losses(svc):
    svc.place_order("ADA", "sell", notional_usd=2000, dry_run=False,
                    confirmation_token=svc.place_order("ADA", "sell", notional_usd=2000, dry_run=True)["confirmation_token"])
    svc.get_account()  # records day-start equity
    svc.backend.move_price("ADA-USD", 15)  # short loses ~300 USD
    acct = svc.get_account()
    assert acct["day_pnl_usd"] < -250
    res = svc.place_order("ADA", "sell", notional_usd=100, dry_run=False)
    assert res["status"] == "blocked"
    assert any(v["rule"] == "daily_loss_limit" for v in res["guardrails"]["violations"])
    assert svc.close_position("ADA")["status"] == "submitted"


def test_deposit_is_not_counted_as_profit(svc):
    svc.place_order("SOL", "buy", notional_usd=100, dry_run=False)
    svc.get_account()  # records day-start equity
    svc.backend.wallet += D("912")  # deposit, positions unchanged
    acct = svc.get_account()
    assert abs(acct["day_pnl_usd"]) < 1
    assert acct["day_external_flows_usd"] == 912


def test_withdrawal_does_not_trip_loss_limit(svc):
    svc.get_account()
    svc.backend.wallet -= D("5000")  # withdrawal, no positions
    acct = svc.get_account()
    assert abs(acct["day_pnl_usd"]) < 1
    assert svc.place_order("ADA", "sell", notional_usd=100, dry_run=False)["status"] != "blocked"


def test_losses_still_count_after_a_deposit(svc):
    token = svc.place_order("ADA", "sell", notional_usd=2000, dry_run=True)["confirmation_token"]
    svc.place_order("ADA", "sell", notional_usd=2000, dry_run=False, confirmation_token=token)
    svc.get_account()
    svc.backend.wallet += D("1000")  # deposit must not cushion the loss limit
    svc.get_account()
    svc.backend.move_price("ADA-USD", 15)  # short loses ~300 USD
    acct = svc.get_account()
    assert acct["day_pnl_usd"] < -250
    assert svc.place_order("ADA", "sell", notional_usd=100, dry_run=False)["status"] == "blocked"


def test_close_retries_with_wider_slippage_on_thin_book(svc):
    svc.place_order("SOL", "buy", notional_usd=100, dry_run=False)
    svc.backend.book_gap["SOL-USD"] = D("0.02")  # book 2% from mark: 0.5% close fails, 3% works
    res = svc.close_position("SOL")
    assert res["status"] == "submitted" and res["retried_with_slippage"] == "0.03"
    assert svc.backend.get_positions() == []


def test_close_reports_clearly_when_market_too_thin(svc):
    svc.place_order("SOL", "buy", notional_usd=100, dry_run=False)
    svc.backend.book_gap["SOL-USD"] = D("0.25")  # like testnet SOL on 21 Oct
    res = svc.close_position("SOL")
    assert res["status"] == "rejected" and "too thin" not in res.get("error", "")
    assert "no liquidity within 3.00%" in res["message"]
    assert len(svc.backend.get_positions()) == 1


def test_close_works_with_kill_switch_and_explicit_slippage(svc):
    svc.place_order("SOL", "buy", notional_usd=100, dry_run=False)
    svc.engage_kill_switch("test")
    svc.backend.book_gap["SOL-USD"] = D("0.015")
    assert svc.close_position("SOL", slippage=0.02)["status"] == "submitted"
    assert svc.close_position("SOL", slippage=0.5)["status"] in ("invalid", "noop")


def test_wide_slippage_only_allowed_for_closes(svc):
    res = svc.place_order("SOL", "buy", notional_usd=100, slippage=0.03, dry_run=True)
    assert res["status"] == "blocked"


def test_invalid_inputs(svc):
    assert svc.place_order("ADA", "sell", dry_run=True)["status"] == "invalid"
    assert svc.place_order("XYZ", "sell", size=1, dry_run=True)["status"] == "invalid"
    assert svc.place_order("ADA", "sell", size=5, dry_run=True)["status"] == "invalid"  # below min notional
    assert svc.place_order("ADA", "hold", size=100, dry_run=True)["status"] == "invalid"
    assert svc.place_order("ADA", "buy", size=100, order_type="limit", dry_run=True)["status"] == "invalid"


def test_limit_price_rounds_conservatively(svc):
    res = svc.place_order("ADA", "buy", size=100, order_type="limit", price=0.71239, dry_run=True)
    assert res["order"]["price"] == "0.7123"
    res = svc.place_order("ADA", "sell", size=100, order_type="limit", price=0.73231, dry_run=True)
    assert res["order"]["price"] == "0.7324"


def test_new_risk_is_capped_to_policy_leverage(svc):
    res = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)
    assert res["order"]["leverage"] == 5 and "lowered" in res["order"]["note"]
    assert svc.backend.get_positions()[0]["leverage"] == 5
    explicit = svc.place_order("ADA", "sell", notional_usd=100, leverage=20, dry_run=True)
    assert explicit["status"] == "blocked"


# ---- human approval mode ------------------------------------------------------

def _approval_mode(svc, **kw):
    path = svc.settings.guardrails_path
    path.write_text(json.dumps({**json.loads(path.read_text()), "require_human_approval": True, **kw}))


def test_new_risk_waits_for_human(svc):
    _approval_mode(svc)
    res = svc.place_order("BTC", "buy", notional_usd=200, dry_run=False, reason="user said BTC looks strong")
    assert res["status"] == "pending_approval" and res["approval_id"]
    assert svc.backend.get_positions() == []  # nothing reached the exchange
    assert svc.get_order_status(res["approval_id"])["status"] == "pending"
    # the agent's confirmation token path cannot bypass it
    again = svc.place_order("BTC", "buy", notional_usd=200, dry_run=False, confirmation_token="anything")
    assert again["status"] == "pending_approval"


def test_human_approves_then_order_executes(svc):
    _approval_mode(svc)
    aid = svc.place_order("ADA", "sell", notional_usd=1500, dry_run=False)["approval_id"]
    out = svc.decide_approval_by_human(aid, approve=True)
    assert out["status"] == "executed"
    assert svc.backend.get_positions()[0]["symbol"] == "ADA-USD"
    st = svc.get_order_status(aid)
    assert st["status"] == "executed" and st["result"]["exchange_status"] == "filled"
    assert svc.decide_approval_by_human(aid, approve=True)["error"]  # cannot execute twice


def test_human_rejects(svc):
    _approval_mode(svc)
    aid = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)["approval_id"]
    assert svc.decide_approval_by_human(aid, approve=False)["status"] == "rejected"
    assert svc.backend.get_positions() == []
    assert svc.get_order_status(aid)["status"] == "rejected"


def test_approval_rechecks_guardrails(svc):
    _approval_mode(svc)
    aid = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)["approval_id"]
    svc.engage_kill_switch("something looks off")
    out = svc.decide_approval_by_human(aid, approve=True)
    assert out["status"] == "blocked_on_approval"
    assert svc.backend.get_positions() == []


def test_closing_never_waits(svc):
    svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)  # opened before approval mode
    _approval_mode(svc)
    assert svc.close_position("ADA")["status"] == "submitted"


def test_auto_approve_small_orders(svc):
    _approval_mode(svc, auto_approve_below_usd=100)
    assert svc.place_order("ADA", "sell", notional_usd=50, dry_run=False)["status"] == "submitted"
    assert svc.place_order("ADA", "sell", notional_usd=150, dry_run=False)["status"] == "pending_approval"


def test_pending_requests_expire(svc, monkeypatch):
    _approval_mode(svc)
    aid = svc.place_order("ADA", "sell", notional_usd=500, dry_run=False)["approval_id"]
    import strike_agent_kit.journal as j
    real = j.time.time
    monkeypatch.setattr(j.time, "time", lambda: real() + 16 * 60)
    assert svc.get_order_status(aid)["status"] == "expired"
    assert "expired" in svc.decide_approval_by_human(aid, approve=True)["error"]
