import pytest

from app.analysis import indicators as ind
from app.data.base import Bar


def test_sma_and_ema_basic():
    values = [1.0, 2, 3, 4, 5]
    assert ind.sma(values, 3) == [None, None, 2.0, 3.0, 4.0]
    e = ind.ema(values, 3)
    assert e[:2] == [None, None]
    assert e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5
    assert e[4] == pytest.approx(4.0)


def test_rsi_extremes():
    up = [float(i) for i in range(30)]
    down = list(reversed(up))
    assert ind.last(ind.rsi(up)) == 100.0
    assert ind.last(ind.rsi(down)) == pytest.approx(0.0)
    flat = [10.0] * 30
    assert ind.last(ind.rsi(flat)) == 50.0


def test_atr_constant_range():
    bars = [Bar(i * 60, 10, 11, 9, 10, 100) for i in range(30)]
    assert ind.last(ind.atr(bars, 14)) == pytest.approx(2.0)


def test_vwap_resets_each_session():
    day1 = 1773149400  # 2026-03-10 09:30 ET
    day2 = day1 + 86400
    bars = [Bar(day1, 10, 10, 10, 10, 100), Bar(day1 + 60, 20, 20, 20, 20, 100),
            Bar(day2, 30, 30, 30, 30, 100)]
    vw = ind.session_vwap(bars)
    assert vw[1] == pytest.approx(15.0)
    assert vw[2] == pytest.approx(30.0)


def test_macd_lengths():
    values = [float(i % 7) for i in range(80)]
    line, sig, hist = ind.macd(values)
    assert len(line) == len(sig) == len(hist) == 80
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])
