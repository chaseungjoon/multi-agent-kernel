"""datekit operations (unimplemented stubs)."""

from __future__ import annotations


def is_leap_year(y: int) -> bool:
    """Whether y is a leap year in the proleptic Gregorian calendar."""
    raise NotImplementedError


def days_in_month(y: int, m: int) -> int:
    """Number of days in month m of year y (1-12)."""
    raise NotImplementedError


def day_of_year(y: int, m: int, d: int) -> int:
    """Ordinal day-of-year (1-366) for the date y-m-d."""
    raise NotImplementedError


def weekday(y: int, m: int, d: int) -> int:
    """Weekday of date y-m-d via Sakamoto's algorithm: 0=Sunday .. 6=Saturday."""
    raise NotImplementedError


def is_valid_date(y: int, m: int, d: int) -> bool:
    """Whether y-m-d is a valid calendar date."""
    raise NotImplementedError


def quarter_of(m: int) -> int:
    """Calendar quarter (1-4) containing month m."""
    raise NotImplementedError


def days_until_year_end(y: int, m: int, d: int) -> int:
    """Number of days from y-m-d to the end of that year."""
    raise NotImplementedError


def month_name(m: int) -> str:
    """English name of month m (1-12)."""
    raise NotImplementedError


def season(m: int) -> str:
    """Northern-hemisphere season for month m (winter/spring/summer/autumn)."""
    raise NotImplementedError


def age_in_years(by: int, bm: int, bd: int, y: int, m: int, d: int) -> int:
    """Whole years from birth date (by,bm,bd) to reference date (y,m,d)."""
    raise NotImplementedError
