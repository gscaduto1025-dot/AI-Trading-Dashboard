from datetime import date, datetime

import pytest

from app.data.base import Bar
from app.market.calendar import NY, MarketCalendar, calendar_from_alpaca, nyse_early_closes, nyse_holidays
from app.market.validation import validate_bars

# Published NYSE schedules.
NYSE_2025 = {date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
             date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
             date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25)}
NYSE_2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
             date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
             date(2026, 11, 26), date(2026, 12, 25)}


def test_holidays_match_published_schedules():
    assert set(nyse_holidays(2025)) == NYSE_2025
    assert set(nyse_holidays(2026)) == NYSE_2026
    assert set(nyse_early_closes(2025)) == {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}
    assert set(nyse_early_closes(2026)) == {date(2026, 11, 27), date(2026, 12, 24)}


def test_observance_rules():
    # 2022: Jan 1 was a Saturday -> no Friday holiday; Christmas Sunday -> Monday Dec 26.
    h22 = nyse_holidays(2022)
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2022, 12, 26) in h22
    assert date(2022, 6, 20) in h22  # Juneteenth Sunday -> Monday
    assert not any(d.month == 6 and d.day in (18, 19, 20) for d in nyse_holidays(2021))


@pytest.mark.parametrize("when,phase", [
    ("2026-03-10T03:59", "closed"),
    ("2026-03-10T04:00", "pre-market"),
    ("2026-03-10T09:29", "pre-market"),
    ("2026-03-10T09:30", "regular"),
    ("2026-03-10T15:59", "regular"),
    ("2026-03-10T16:00", "after-hours"),
    ("2026-03-10T20:00", "closed"),
    ("2026-03-14T12:00", "closed"),  # Saturday
    ("2026-04-03T12:00", "closed"),  # Good Friday
    ("2026-11-27T12:59", "regular"),  # early close day
    ("2026-11-27T13:00", "after-hours"),
    ("2026-11-27T17:00", "closed"),
])
def test_session_phases(when, phase):
    now = datetime.fromisoformat(when).replace(tzinfo=NY)
    assert MarketCalendar().status(now)["phase"] == phase


def test_dst_transition_keeps_new_york_open_time():
    cal = MarketCalendar()
    before = cal.day(date(2026, 3, 6)).open  # EST, UTC-5
    after = cal.day(date(2026, 3, 9)).open  # EDT, UTC-4
    assert before.utcoffset().total_seconds() == -5 * 3600
    assert after.utcoffset().total_seconds() == -4 * 3600
    assert before.strftime("%H:%M") == after.strftime("%H:%M") == "09:30"


def test_alpaca_calendar_overrides_rules():
    rows = [{"date": "2026-03-10", "open": "09:30", "close": "13:00",
             "session_open": "0400", "session_close": "1700"}]
    cal = calendar_from_alpaca(rows, date(2026, 3, 9), date(2026, 3, 11))
    assert cal.day(date(2026, 3, 9)) is None  # missing from Alpaca -> closed
    assert cal.day(date(2026, 3, 10)).close.strftime("%H:%M") == "13:00"
    assert cal.day(date(2026, 3, 12)) is not None  # outside the range -> rules


T0 = int(datetime(2026, 3, 10, 9, 30, tzinfo=NY).timestamp())


def bar(t, o=10, h=11, l=9, c=10.5, v=100):
    return Bar(t, o, h, l, c, v)


def test_validation_flags_bad_ohlc_order_alignment_and_future():
    bars = [bar(T0), bar(T0 + 300, h=8), bar(T0 + 900), bar(T0 + 600), bar(T0 + 1201),
            bar(T0 + 300 * 4000), bar(T0 + 1200, v=-1), bar(T0 + 1500, l=0)]
    clean, rep = validate_bars(bars, align_seconds=300, now=T0 + 3600)
    assert [b.time for b in clean] == [T0, T0 + 600, T0 + 900]
    joined = " ".join(rep.errors)
    for needle in ("high below", "Out-of-order", "Misaligned", "future", "negative volume",
                   "non-positive"):
        assert needle in joined
    assert not rep.ok


def test_validation_duplicates():
    clean, rep = validate_bars([bar(T0), bar(T0), bar(T0 + 300)], align_seconds=300, now=T0 + 900)
    assert len(clean) == 2 and rep.ok and rep.warnings
    clean, rep = validate_bars([bar(T0), bar(T0, c=10.9)], align_seconds=300, now=T0 + 900)
    assert not rep.ok and "Conflicting duplicate" in rep.errors[0]


def test_validation_rejects_nan():
    clean, rep = validate_bars([bar(T0, c=float("nan"))], align_seconds=None, now=T0)
    assert clean == [] and not rep.ok
