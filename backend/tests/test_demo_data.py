from datetime import timedelta

from app.market.calendar import NY
from tests.conftest import FIXED_NOW, demo_market


def test_demo_is_deterministic_and_flagged():
    a = demo_market().get_candles("SPY", "5Min", limit=100)
    b = demo_market().get_candles("SPY", "5Min", limit=100)
    assert a.bars == b.bars
    assert len(a.bars) == 100
    assert a.status["latency"] == "demo" and a.status["feed"] == "demo"


def test_all_timeframes_sane_and_ordered():
    m = demo_market()
    for tf in ("1Min", "5Min", "15Min", "30Min", "1Hour", "4Hour", "1Day", "1Week"):
        c = m.get_candles("NVDA", tf, limit=120)
        assert c.bars, tf
        assert c.quality.ok, (tf, c.quality.errors)
        times = [b.time for b in c.bars]
        assert times == sorted(set(times)), tf
        for b in c.bars:
            assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        if tf == "1Week":
            # FIXED_NOW is a Tuesday: the current week's candle is still forming.
            assert all(c.complete[:-1]) and not c.complete[-1]
        else:
            assert all(c.complete), tf  # demo data ends at a completed session


def test_demo_skips_holidays_and_respects_early_close():
    m = demo_market()
    days = {b.time for b in m.get_candles("SPY", "1Day", limit=300).bars}
    from datetime import date, datetime
    stamp = lambda d: int(datetime.combine(d, datetime.min.time(), NY).timestamp())  # noqa: E731
    assert stamp(date(2025, 12, 25)) not in days  # Christmas
    assert stamp(date(2025, 12, 24)) in days  # early close day still trades
    intraday = m.get_history("SPY", "30Min", datetime(2025, 12, 24, tzinfo=NY),
                             datetime(2025, 12, 25, tzinfo=NY))
    last = max(intraday.bars, key=lambda b: b.time)
    assert datetime.fromtimestamp(last.time, NY).strftime("%H:%M") == "12:30"  # 13:00 close


def test_quote_matches_daily_close():
    m = demo_market()
    q = {x["symbol"]: x for x in m.get_quotes()}["AAPL"]
    assert q["available"]
    assert q["price"] == m.get_candles("AAPL", "1Day", limit=5).bars[-1].close
    assert q["price"] == m.get_candles("AAPL", "5Min", limit=5).bars[-1].close
    assert FIXED_NOW - timedelta(days=5) < FIXED_NOW
