from decimal import Decimal as D

from strike_agent_kit.guardrails import AccountState, OrderIntent, Policy, evaluate

MARKS = {"ADA-USD": D("0.72"), "BTC-USD": D("95000"), "DOGE-USD": D("0.2")}


def policy(**kw):
    base = dict(allowed_markets=["ADA-USD", "BTC-USD"], max_leverage=5, max_order_notional_usd=D(2500),
                max_position_notional_usd=D(5000), max_total_notional_usd=D(10000),
                daily_loss_limit_usd=D(250), max_slippage=D("0.01"), confirm_above_usd=D(1000))
    base.update(kw)
    return Policy(**base)


def state(positions=None, day_pnl=0, lev=None):
    return AccountState(positions=positions or {}, marks=MARKS, day_pnl_usd=D(day_pnl), current_leverage=lev or {})


def intent(symbol="ADA-USD", side="sell", size="1000", **kw):
    return OrderIntent(symbol, side, kw.pop("order_type", "market"), D(size), MARKS[symbol], **kw)


def rules(d):
    return {v.rule for v in d.violations}


def test_small_order_allowed_without_confirmation():
    d = evaluate(policy(), intent(size="1000"), state())  # 720 USD
    assert d.allowed and d.risk_increasing and not d.needs_confirmation


def test_large_order_needs_confirmation():
    d = evaluate(policy(), intent(size="2000"), state())  # 1440 USD
    assert d.allowed and d.needs_confirmation


def test_order_notional_cap():
    d = evaluate(policy(), intent(size="4000"), state())  # 2880 USD
    assert not d.allowed and "max_order_notional" in rules(d)


def test_position_cap_counts_existing_position():
    d = evaluate(policy(), intent(size="3000"), state({"ADA-USD": D("-5000")}))  # 8000 ADA = 5760 USD
    assert "max_position_notional" in rules(d)


def test_total_exposure_cap():
    d = evaluate(policy(), intent(size="3000"), state({"BTC-USD": D("0.1")}))  # 9500 + 2160
    assert "max_total_notional" in rules(d)


def test_leverage_cap_on_new_risk():
    d = evaluate(policy(), intent(leverage=10), state())
    assert "max_leverage" in rules(d)


def test_existing_high_leverage_blocks_adding():
    d = evaluate(policy(), intent(), state(lev={"ADA-USD": 20}))
    assert "max_leverage" in rules(d)


def test_disallowed_market_blocks_new_risk():
    d = evaluate(policy(), intent(symbol="DOGE-USD", size="100"), state())
    assert "allowed_markets" in rules(d)


def test_kill_switch_blocks_new_risk_but_allows_closing():
    p = policy(kill_switch=True)
    assert "kill_switch" in rules(evaluate(p, intent(), state()))
    closing = evaluate(p, intent(side="buy", size="1000", reduce_only=True), state({"ADA-USD": D("-1000")}))
    assert closing.allowed and not closing.risk_increasing and closing.position_after == 0


def test_daily_loss_limit_blocks_new_risk_only():
    s = state({"ADA-USD": D("-1000")}, day_pnl=-300)
    assert "daily_loss_limit" in rules(evaluate(policy(), intent(), s))
    assert evaluate(policy(), intent(side="buy", size="500"), s).allowed


def test_flip_counts_as_risk_increasing():
    d = evaluate(policy(kill_switch=True), intent(side="buy", size="1500"), state({"ADA-USD": D("-1000")}))
    assert d.risk_increasing and "kill_switch" in rules(d)


def test_reduce_only_in_wrong_direction_has_no_effect():
    d = evaluate(policy(), intent(side="sell", size="100", reduce_only=True), state({"ADA-USD": D("-1000")}))
    assert "no_effect" in rules(d)


def test_slippage_cap():
    d = evaluate(policy(), intent(slippage=D("0.05")), state())
    assert "max_slippage" in rules(d)


def test_order_type_whitelist():
    d = evaluate(policy(allowed_order_types=["market"]), intent(order_type="limit"), state())
    assert "allowed_order_types" in rules(d)
