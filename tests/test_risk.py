from datetime import datetime

from agent.clock import IST
from agent.config import ExecutionCfg, RiskCfg
from agent.risk import RiskManager, StopManager


def test_lot_sizing():
    rm = RiskManager(RiskCfg(capital=100000, risk_per_trade_pct=1.0, stop_loss_pct=25, max_lots=3, min_lots=1),
                     ExecutionCfg(live_max_lots=1))
    # premium 100, stop 25% -> risk/lot = 25*65 = 1625 ; budget 1000 -> floor 0 -> min 1 lot (1625 <= 2*1000 ok)
    assert rm.lots_for(100, 65, live=False) == 1
    # premium 20 -> risk/lot 325 -> 3 lots (cap)
    assert rm.lots_for(20, 65, live=False) == 3
    assert rm.lots_for(20, 65, live=True) == 1
    # premium 200 -> risk/lot 3250 > 2x budget -> 0
    assert rm.lots_for(200, 65, live=False) == 0


def test_can_enter_rules():
    rm = RiskManager(RiskCfg(capital=100000, max_daily_loss_pct=2, min_premium=20, max_premium=400, square_off_time="15:10"),
                     ExecutionCfg())
    t = datetime(2026, 10, 27, 10, 0, tzinfo=IST)
    assert rm.can_enter(t, 0, 100) is None
    assert "daily loss" in rm.can_enter(t, -2000, 100)
    assert "below min" in rm.can_enter(t, 0, 10)
    assert "above max" in rm.can_enter(t, 0, 500)
    assert "square-off" in rm.can_enter(datetime(2026, 10, 27, 15, 11, tzinfo=IST), 0, 100)


def test_stop_manager_breakeven_trail_target():
    cfg = RiskCfg(stop_loss_pct=25, target_pct=50, breakeven_at_pct=15, trail_activate_pct=25, trail_step_pct=12)
    sm = StopManager.new(cfg, 100.0)
    assert sm.s.stop == 75.0 and sm.s.target == 150.0
    assert sm.update(90) is None
    assert sm.update(116) is None and sm.s.stop == 100.0 and sm.s.breakeven_done
    assert sm.update(130) is None and sm.s.trailing and sm.s.stop == round(130 * 0.88, 2)
    assert sm.update(140) is None and sm.s.stop == round(140 * 0.88, 2)
    assert sm.update(122) is not None and sm.update(122).startswith("STOPLOSS")  # 122 <= 123.2
    sm2 = StopManager.new(cfg, 100.0)
    assert sm2.update(151).startswith("TARGET")
    sm3 = StopManager.new(cfg, 100.0)
    assert sm3.update(74.9).startswith("STOPLOSS")


def test_points_based_lot_sizing():
    # Capital 100,000, risk_per_trade_pct 2.0 -> ₹2,000 budget
    cfg_nifty = RiskCfg(capital=100000, risk_per_trade_pct=2.0, stop_loss_points=25, target_points=25, max_lots=2, min_lots=1)
    rm_nifty = RiskManager(cfg_nifty, ExecutionCfg(live_max_lots=1))
    # NIFTY: lot size 65, 25 pts SL -> risk/lot = 1625 <= 2000 -> 1 lot
    assert rm_nifty.lots_for(120, 65, live=False) == 1

    # SENSEX: lot size 20, 50 pts SL -> risk/lot = 1000 -> 2 lots (2000 total risk)
    cfg_sensex = RiskCfg(capital=100000, risk_per_trade_pct=2.0, stop_loss_points=50, target_points=50, max_lots=2, min_lots=1)
    rm_sensex = RiskManager(cfg_sensex, ExecutionCfg(live_max_lots=1))
    assert rm_sensex.lots_for(500, 20, live=False) == 2
    assert rm_sensex.lots_for(500, 20, live=True) == 1  # clamped by live_max_lots

    # BANKNIFTY: lot size 15, 50 pts SL -> risk/lot = 750 -> 2 lots
    cfg_bn = RiskCfg(capital=100000, risk_per_trade_pct=2.0, stop_loss_points=50, target_points=50, max_lots=2, min_lots=1)
    rm_bn = RiskManager(cfg_bn, ExecutionCfg(live_max_lots=1))
    assert rm_bn.lots_for(400, 15, live=False) == 2


def test_stop_manager_points():
    # NIFTY test: target 25 pts, SL 25 pts, breakeven 15 pts, trail activate 20 pts, trail step 10 pts
    cfg = RiskCfg(target_points=25, stop_loss_points=25, breakeven_points=15, trail_activate_points=20, trail_step_points=10)
    sm = StopManager.new(cfg, 100.0)
    assert sm.s.stop == 75.0 and sm.s.target == 125.0
    assert sm.update(110) is None
    # Hit breakeven at +15 pts (115)
    assert sm.update(115) is None and sm.s.stop == 100.0 and sm.s.breakeven_done
    # Hit trailing at +20 pts (120), trail step 10 -> stop is 110
    assert sm.update(120) is None and sm.s.trailing and sm.s.stop == 110.0
    # Stop loss triggered if drops to 110
    assert sm.update(109.9).startswith("STOPLOSS")

    # SENSEX test: target 50 pts, SL 50 pts
    cfg_sx = RiskCfg(target_points=50, stop_loss_points=50)
    sm2 = StopManager.new(cfg_sx, 500.0)
    assert sm2.s.stop == 450.0 and sm2.s.target == 550.0
    assert sm2.update(550.0).startswith("TARGET")
    sm3 = StopManager.new(cfg_sx, 500.0)
    assert sm3.update(450.0).startswith("STOPLOSS")
