"""straddle_trend_switch is enabled for LIVE orders; LIVE still needs a loss limit and the typed confirmation."""
from algolab.strategies.straddle_trend_switch import StraddleTrendSwitch


def test_straddle_trend_switch_is_live_ready():
    assert StraddleTrendSwitch.live_ready is True
