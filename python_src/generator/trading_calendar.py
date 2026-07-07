"""Trading calendar (generator-spec.md §4.1): all weekdays, no holidays."""

from __future__ import annotations

from datetime import date

import numpy as np

BUSDAYS_PER_YEAR = 261.0  # weekday count per year, used for daily hazards


def trading_days(start: date, end: date) -> np.ndarray:
    """All weekdays in [start, end] as datetime64[D]."""
    days = np.arange(np.datetime64(start, "D"), np.datetime64(end, "D") + 1)
    return days[np.is_busday(days)]


def years_of(days: np.ndarray) -> np.ndarray:
    """Calendar year of each trading day, as int."""
    return days.astype("datetime64[Y]").astype(int) + 1970
