import pytest

from agent.config import Config


def test_apply_underlying_sets_exchanges_and_overrides():
    cfg = Config(underlyings={"SENSEX": {"risk": {"max_premium": 900}}})
    cfg.apply_underlying("sensex")
    assert cfg.instrument.underlying == "SENSEX"
    assert cfg.instrument.underlying_exchange == "BSE_INDEX" and cfg.instrument.option_exchange == "BFO"
    assert cfg.risk.max_premium == 900
    cfg.apply_underlying("BANKNIFTY")
    assert cfg.instrument.option_exchange == "NFO" and cfg.risk.max_premium == 900  # overrides are cumulative per run
    with pytest.raises(ValueError):
        cfg.apply_underlying("GOLD")
    with pytest.raises(ValueError):
        Config(underlyings={"NIFTY": {"risk": {"no_such": 1}}}).apply_underlying("NIFTY")
