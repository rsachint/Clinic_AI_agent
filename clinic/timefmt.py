"""Display helpers for timestamps. SQLite's datetime('now') stores UTC; the
clinic works in India Standard Time (UTC+05:30, no daylight saving), so
anything shown to staff that came from a UTC column goes through here."""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ")


def utc_to_ist(text):
    """'2026-10-03 06:34:16' (UTC) -> '2026-10-03 12:04:16' (IST).
    Anything empty or unparseable is returned unchanged rather than raising,
    so a surprising value never breaks a page render."""
    if not text:
        return text
    raw = str(text).strip()
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    return text
