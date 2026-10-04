"""Tiny key/value settings store (the additive `app_settings` table).

Only the automatic-appointment controls live here today:

  auto_appointments_enabled  '1' (default) / '0'  -- the kill switch. When off,
                             every patient request goes to the staff inbox as a
                             proposal, exactly as before automation existed.
  auto_daily_cap             integer, default 40  -- how many automated
                             BOOKINGS may be created per calendar day (a flood
                             guard, not a per-appointment-date capacity).

Reads never raise: a missing table or row simply yields the default (so an
old connection or a half-migrated database behaves like "defaults"). Writes
validate and raise ValueError on nonsense.
"""

import sqlite3
from datetime import datetime

AUTO_ENABLED = "auto_appointments_enabled"
AUTO_DAILY_CAP = "auto_daily_cap"

DEFAULTS = {
    AUTO_ENABLED: "1",
    AUTO_DAILY_CAP: "40",
}

MAX_DAILY_CAP = 1000


def get(conn, key):
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None or row[0] is None:
        return DEFAULTS.get(key)
    return row[0]


def set_value(conn, key, value):
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def auto_enabled(conn):
    return str(get(conn, AUTO_ENABLED)).strip() != "0"


def set_auto_enabled(conn, enabled):
    set_value(conn, AUTO_ENABLED, "1" if enabled else "0")


def auto_daily_cap(conn):
    try:
        return max(0, int(get(conn, AUTO_DAILY_CAP)))
    except (TypeError, ValueError):
        return int(DEFAULTS[AUTO_DAILY_CAP])


def set_auto_daily_cap(conn, cap):
    try:
        number = int(cap)
    except (TypeError, ValueError):
        raise ValueError("The daily cap must be a whole number.")
    if isinstance(cap, bool) or not 0 <= number <= MAX_DAILY_CAP:
        raise ValueError("The daily cap must be between 0 and {}.".format(MAX_DAILY_CAP))
    set_value(conn, AUTO_DAILY_CAP, number)
