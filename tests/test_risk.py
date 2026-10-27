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
