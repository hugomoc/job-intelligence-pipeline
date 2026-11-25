"""Jobs-tab End date default: follow the latest completed ingestion run.

Pure session-state logic (no Streamlit or DB imports) so reruns, manual
overrides and timezone boundaries are testable offline.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("America/Los_Angeles")
# Remembers which completed run last set the End date, and the date it chose.
END_DATE_RUN_MARKER_KEY = "email_end_date_ingestion_run"


def _datetime_date(value: datetime, tz) -> date:
    # Stored timestamps are UTC; a naive value is therefore UTC, never local.
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz).date()  # zoneinfo applies PST/PDT per instant


def pacific_date(value: object, tz=APP_TIMEZONE) -> date | None:
    """Jobs-filter calendar date in the app timezone, for options and filtering.

    Datetimes (aware, or naive = UTC) convert to Pacific; a plain ``date`` or a
    date-only string is already a calendar date. Comparing these dates with an
    inclusive End date covers that whole Pacific day, through 23:59:59.999999.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _datetime_date(value, tz)
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return _datetime_date(datetime.fromisoformat(text), tz)
    except ValueError:
        try:
            return date.fromisoformat(text.split()[0])  # previous lenient fallback
        except ValueError:
            return None


def pacific_today(tz=APP_TIMEZONE) -> date:
    return datetime.now(tz).date()


def local_run_date(completed_at: datetime | None, tz=APP_TIMEZONE) -> date | None:
    """Calendar date of a completion timestamp in the app timezone (not UTC)."""
    if not isinstance(completed_at, datetime):
        return None
    return _datetime_date(completed_at, tz)


def end_date_for_run(available_dates: list[date], run_date: date | None) -> date | None:
    """Latest selectable date on or before the run date, or None (keep fallback)."""
    if run_date is None:
        return None
    eligible = [value for value in available_dates if value <= run_date]
    return max(eligible) if eligible else None


def apply_latest_ingestion_end_date(state, *, key: str, available_dates: list[date],
                                    latest_run: tuple[str, datetime] | None, tz=APP_TIMEZONE) -> bool:
    """Advance ``state[key]`` once per newly completed ingestion run.

    - Startup / new run id: set End date to that run's local date (snapped to
      an available option).
    - Same run id on later reruns: leave the user's selection alone, except to
      finish an earlier snap if a closer date became available and the user has
      not changed the value since.
    - No reliable run, or no option on/before its date: change nothing, so the
      existing default (latest available date) applies.
    Returns True when the value was set.
    """
    if latest_run is None or not available_dates:
        return False
    run_id, completed_at = latest_run
    target = end_date_for_run(available_dates, local_run_date(completed_at, tz))
    if target is None:
        return False
    marker = state.get(END_DATE_RUN_MARKER_KEY)
    if isinstance(marker, dict) and marker.get("run_id") == run_id:
        if marker.get("applied") == target or state.get(key) != marker.get("applied"):
            return False  # already applied, or manually overridden since
    state[key] = target
    state[END_DATE_RUN_MARKER_KEY] = {"run_id": run_id, "applied": target}
    return True
