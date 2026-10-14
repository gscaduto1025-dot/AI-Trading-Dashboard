"""US equity market calendar in America/New_York time.

Two sources, used in this order:
1. Alpaca's trading calendar (GET /v2/calendar on the Trading API), when credentials are
   configured. It is authoritative and covers unscheduled closures.
2. Built-in NYSE rules: the full holiday list with weekend observance rules, Good Friday,
   Juneteenth (from 2022), the 1:00 PM early closes, and known special closures.

Session times: pre-market 04:00-09:30, regular 09:30-16:00 (13:00 on early-close days),
after-hours until 20:00 (17:00 on early-close days).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)
EARLY_AFTER_HOURS_CLOSE = time(17, 0)

# Unscheduled full-day closures that rules cannot predict.
SPECIAL_CLOSURES: dict[date, str] = {
    date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "National Day of Mourning (George H. W. Bush)",
    date(2025, 1, 9): "National Day of Mourning (Jimmy Carter)",
}


@dataclass(frozen=True)
class SessionDay:
    """One trading day. All datetimes are timezone-aware (America/New_York)."""

    day: date
    open: datetime
    close: datetime
    session_open: datetime  # pre-market start
    session_close: datetime  # after-hours end
    early_close: bool = False

    def bounds(self, session: str) -> tuple[datetime, datetime]:
        if session == "extended":
            return self.session_open, self.session_close
        return self.open, self.close

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "open": self.open.isoformat(),
            "close": self.close.isoformat(),
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "early_close": self.early_close,
        }


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, NY)


def make_session_day(d: date, open_t: time = REGULAR_OPEN, close_t: time = REGULAR_CLOSE,
                     pre_t: time = PRE_MARKET_OPEN, post_t: time | None = None) -> SessionDay:
    early = close_t < REGULAR_CLOSE
    if post_t is None:
        post_t = EARLY_AFTER_HOURS_CLOSE if early else AFTER_HOURS_CLOSE
    return SessionDay(d, _at(d, open_t), _at(d, close_t), _at(d, pre_t), _at(d, post_t), early)


# ------------------------------------------------------------------- NYSE rule set

def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> dict[date, str]:
    h: dict[date, str] = {}
    new_year = date(year, 1, 1)
    # NYSE does not close on Friday Dec 31 when Jan 1 falls on a Saturday.
    if new_year.weekday() != 5:
        h[_observed(new_year)] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    h[_observed(date(year, 12, 25))] = "Christmas Day"
    for d, name in SPECIAL_CLOSURES.items():
        if d.year == year:
            h[d] = name
    return h


@lru_cache(maxsize=64)
def nyse_early_closes(year: int) -> dict[date, str]:
    holidays = nyse_holidays(year)
    out: dict[date, str] = {}
    july3 = date(year, 7, 3)
    if july3.weekday() < 4 and july3 not in holidays:  # Mon-Thu only
        out[july3] = "Independence Day eve"
    out[_nth_weekday(year, 11, 3, 4) + timedelta(days=1)] = "Day after Thanksgiving"
    xmas_eve = date(year, 12, 24)
    if xmas_eve.weekday() < 4 and xmas_eve not in holidays:
        out[xmas_eve] = "Christmas Eve"
    return out


# --------------------------------------------------------------------- the calendar

class MarketCalendar:
    """Trading-day lookups. `days` (e.g. from Alpaca) overrides the rules inside `covered`."""

    def __init__(self, days: dict[date, SessionDay] | None = None,
                 covered: tuple[date, date] | None = None, source: str = "nyse-rules") -> None:
        self._days = days or {}
        self._covered = covered
        self.source = source

    def _in_override(self, d: date) -> bool:
        return self._covered is not None and self._covered[0] <= d <= self._covered[1]

    def day(self, d: date) -> SessionDay | None:
        if self._in_override(d):
            return self._days.get(d)
        if d.weekday() >= 5 or d in nyse_holidays(d.year):
            return None
        if d in nyse_early_closes(d.year):
            return make_session_day(d, close_t=EARLY_CLOSE)
        return make_session_day(d)

    def is_trading_day(self, d: date) -> bool:
        return self.day(d) is not None

    def closure_reason(self, d: date) -> str | None:
        if self.is_trading_day(d):
            return None
        if d.weekday() >= 5:
            return "Weekend"
        return nyse_holidays(d.year).get(d, "Market holiday")

    def early_close_reason(self, d: date) -> str | None:
        sd = self.day(d)
        if sd is None or not sd.early_close:
            return None
        return nyse_early_closes(d.year).get(d, "Early close")

    def previous_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def trading_days(self, start: date, end: date) -> list[SessionDay]:
        out, d = [], start
        while d <= end:
            sd = self.day(d)
            if sd:
                out.append(sd)
            d += timedelta(days=1)
        return out

    def trading_days_back(self, end: date, count: int) -> list[SessionDay]:
        """The `count` most recent trading days on or before `end`, oldest first."""
        out: list[SessionDay] = []
        d = end
        while len(out) < count:
            sd = self.day(d)
            if sd:
                out.append(sd)
            d -= timedelta(days=1)
        return list(reversed(out))

    def session_for(self, moment: datetime) -> SessionDay | None:
        return self.day(moment.astimezone(NY).date())

    def last_session_started(self, now: datetime, session: str = "regular") -> SessionDay:
        """Most recent trading day whose `session` has begun at `now`."""
        d = now.astimezone(NY).date()
        sd = self.day(d)
        if sd and now >= sd.bounds(session)[0]:
            return sd
        prev = self.day(self.previous_trading_day(d))
        assert prev is not None
        return prev

    def last_completed_session(self, now: datetime) -> SessionDay:
        """Most recent trading day whose regular session has closed at `now`."""
        d = now.astimezone(NY).date()
        sd = self.day(d)
        if sd and now >= sd.close:
            return sd
        prev = self.day(self.previous_trading_day(d))
        assert prev is not None
        return prev

    def status(self, now: datetime) -> dict:
        """Market session status at `now` (any timezone)."""
        now = now.astimezone(NY)
        today = now.date()
        sd = self.day(today)
        if sd is None:
            phase, label = "closed", f"Closed — {self.closure_reason(today)}"
        elif now < sd.session_open:
            phase, label = "closed", "Closed — overnight"
        elif now < sd.open:
            phase, label = "pre-market", "Pre-market"
        elif now < sd.close:
            phase, label = "regular", "Regular session"
        elif now < sd.session_close:
            phase, label = "after-hours", "After-hours"
        else:
            phase, label = "closed", "Closed — overnight"

        upcoming = self.day(self.next_trading_day(today))
        assert upcoming is not None
        next_open = sd.open if sd is not None and now < sd.open else upcoming.open
        next_close = sd.close if sd is not None and now < sd.close else upcoming.close
        early = self.early_close_reason(today)
        return {
            "phase": phase,
            "label": label,
            "is_open": phase == "regular",
            "is_extended_hours": phase in ("pre-market", "after-hours"),
            "trading_day": sd is not None,
            "holiday": None if sd else self.closure_reason(today),
            "early_close": early,
            "today": sd.to_dict() if sd else None,
            "next_open": next_open.isoformat(),
            "next_close": next_close.isoformat(),
            "now": now.isoformat(),
            "timezone": "America/New_York",
            "calendar_source": self.source,
        }


def calendar_from_alpaca(rows: list[dict], start: date, end: date) -> MarketCalendar:
    """Build a calendar from Alpaca /v2/calendar rows.

    Rows look like {"date": "2025-11-28", "open": "09:30", "close": "13:00",
    "session_open": "0400", "session_close": "1700"}; session fields may be absent.
    """
    def hm(value: str | None, default: time) -> time:
        if not value:
            return default
        value = value.replace(":", "")
        return time(int(value[:2]), int(value[2:4]))

    days: dict[date, SessionDay] = {}
    for row in rows:
        d = date.fromisoformat(row["date"])
        open_t = hm(row.get("open"), REGULAR_OPEN)
        close_t = hm(row.get("close"), REGULAR_CLOSE)
        post_default = EARLY_AFTER_HOURS_CLOSE if close_t < REGULAR_CLOSE else AFTER_HOURS_CLOSE
        days[d] = make_session_day(d, open_t, close_t, hm(row.get("session_open"), PRE_MARKET_OPEN),
                                   hm(row.get("session_close"), post_default))
    return MarketCalendar(days, (start, end), source="alpaca")
