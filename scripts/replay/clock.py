"""A fixed clock for the replay: the clinic's "today" is 2026-10-09 (a Friday), 10:00 IST, whatever the machine says.

`date.today()` and `datetime.now()` are read by many modules (`from datetime import date` at the top of each,
sometimes inside a function), so the `datetime` module's own `date` and `datetime` names are replaced while the
replay runs AND every already-imported clinic module that holds one of them. Only the two clock readers change;
every other date / datetime behaviour is the real one (the replacements are subclasses that build real objects).
"""

import contextlib
import datetime as _dt
import sys

REAL_DATE = _dt.date
REAL_DATETIME = _dt.datetime

FIXED_DAY = REAL_DATE(2026, 10, 9)                       # Friday
FIXED_NOW = REAL_DATETIME(2026, 10, 9, 10, 0, 0)         # IST wall clock
IST = _dt.timedelta(hours=5, minutes=30)


class _DateMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, REAL_DATE)


class _DatetimeMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, REAL_DATETIME)


def make_classes(day=FIXED_DAY, now=FIXED_NOW):
    class FrozenDate(REAL_DATE, metaclass=_DateMeta):
        def __new__(cls, *args, **kwargs):
            return REAL_DATE(*args, **kwargs)

        @classmethod
        def today(cls):
            return day

        @classmethod
        def fromisoformat(cls, text):
            return REAL_DATE.fromisoformat(text)

        @classmethod
        def fromordinal(cls, n):
            return REAL_DATE.fromordinal(n)

    class FrozenDatetime(REAL_DATETIME, metaclass=_DatetimeMeta):
        def __new__(cls, *args, **kwargs):
            return REAL_DATETIME(*args, **kwargs)

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now
            return (now - IST).replace(tzinfo=_dt.timezone.utc).astimezone(tz)

        @classmethod
        def today(cls):
            return now

        @classmethod
        def utcnow(cls):
            return now - IST

        @classmethod
        def fromisoformat(cls, text):
            return REAL_DATETIME.fromisoformat(text)

        @classmethod
        def strptime(cls, text, fmt):
            return REAL_DATETIME.strptime(text, fmt)

        @classmethod
        def combine(cls, *args, **kwargs):
            return REAL_DATETIME.combine(*args, **kwargs)

        @classmethod
        def fromtimestamp(cls, *args, **kwargs):
            return REAL_DATETIME.fromtimestamp(*args, **kwargs)

    return FrozenDate, FrozenDatetime


@contextlib.contextmanager
def frozen(day=FIXED_DAY, now=FIXED_NOW):
    """While active the clinic's clock reads `day` / `now`. Restores everything on exit."""
    frozen_date, frozen_datetime = make_classes(day, now)
    patched = []                                           # (namespace, name, original)

    def swap(namespace, name, new):
        patched.append((namespace, name, namespace[name]))
        namespace[name] = new

    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "") or ""
        if not (name == "clinic" or name.startswith("clinic.") or name == "scripts.replay.engine"):
            continue
        namespace = getattr(module, "__dict__", None)
        if not namespace:
            continue
        for key, value in list(namespace.items()):
            if value is REAL_DATE:
                swap(namespace, key, frozen_date)
            elif value is REAL_DATETIME:
                swap(namespace, key, frozen_datetime)
    # code that imports the names inside a function (`from datetime import date as _date`) reads the module
    module_ns = _dt.__dict__
    patched.append((module_ns, "date", module_ns["date"]))
    patched.append((module_ns, "datetime", module_ns["datetime"]))
    module_ns["date"], module_ns["datetime"] = frozen_date, frozen_datetime
    try:
        yield
    finally:
        for namespace, key, original in reversed(patched):
            namespace[key] = original
