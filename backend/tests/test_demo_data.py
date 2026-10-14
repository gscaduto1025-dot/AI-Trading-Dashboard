from app.data.demo import DemoDataProvider
from tests.conftest import FIXED_DAY


def test_demo_is_deterministic_and_flagged():
    a = DemoDataProvider(today=FIXED_DAY).get_bars("SPY", "5Min", 100)
    b = DemoDataProvider(today=FIXED_DAY).get_bars("SPY", "5Min", 100)
    assert a == b
    assert DemoDataProvider.is_demo is True
    assert len(a) == 100


def test_ohlc_sane_and_ordered():
    for tf in ("1Min", "5Min", "15Min", "1Hour", "1Day"):
        bars = DemoDataProvider(today=FIXED_DAY).get_bars("NVDA", tf, 200)
        assert [b.time for b in bars] == sorted(b.time for b in bars)
        for b in bars:
            assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
            assert b.volume > 0


def test_timeframes_agree_with_watchlist_quote():
    p = DemoDataProvider(today=FIXED_DAY)
    quote = p.get_quotes(["AAPL"])[0]
    assert p.get_bars("AAPL", "1Day", 5)[-1].close == quote.price
    assert p.get_bars("AAPL", "5Min", 5)[-1].close == quote.price
