"""An in-memory Google Calendar for tests: the same four-method interface as
clinic/gcal_client.CalendarClient, plus switches to make it fail and helpers
that play the part of a person editing the calendar by hand. No network."""
import copy
from datetime import datetime

from clinic.gcal_client import GcalApiError


class FakeCalendar:
    def __init__(self):
        self.events = {}        # event id -> event dict
        self._calendar_of = {}  # event id -> calendar id it lives in
        self.calls = []         # (op, calendar_id, ...) in call order
        self._next_id = 1
        self._fail_next = {}    # op -> [exception, ...]
        self._fail_always = {}  # op -> exception
        self.before_patch = None   # callable(event_id): runs just before a patch is applied

    # -- failure switches ------------------------------------------------
    def fail_next(self, op, exc, times=1):
        self._fail_next.setdefault(op, []).extend([exc] * times)

    def fail_always(self, op, exc):
        self._fail_always[op] = exc

    def heal(self):
        self._fail_next.clear()
        self._fail_always.clear()

    def _maybe_fail(self, op):
        queued = self._fail_next.get(op)
        if queued:
            raise queued.pop(0)
        if op in self._fail_always:
            raise self._fail_always[op]

    # -- the client interface ----------------------------------------------
    def list_events(self, calendar_id, time_min, time_max):
        self.calls.append(("list", calendar_id, time_min, time_max))
        self._maybe_fail("list")
        lo, hi = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)
        out = []
        for event in self.events.values():
            if self._calendar_of[event["id"]] != calendar_id:
                continue
            private = (event.get("extendedProperties") or {}).get("private") or {}
            if private.get("source") != "clinic-copilot":
                continue  # the real API filters on privateExtendedProperty
            start = datetime.fromisoformat(event["start"]["dateTime"])
            end = datetime.fromisoformat(event["end"]["dateTime"])
            if start < hi and end > lo:
                out.append(copy.deepcopy(event))
        return out

    def insert_event(self, calendar_id, body):
        self.calls.append(("insert", calendar_id, body["summary"]))
        self._maybe_fail("insert")
        event = copy.deepcopy(body)
        event["id"] = "evt{}".format(self._next_id)
        self._next_id += 1
        self.events[event["id"]] = event
        self._calendar_of[event["id"]] = calendar_id
        return copy.deepcopy(event)

    def patch_event(self, calendar_id, event_id, body):
        self.calls.append(("patch", calendar_id, event_id))
        self._maybe_fail("patch")
        if self.before_patch:
            self.before_patch(event_id)
        if event_id not in self.events or self._calendar_of[event_id] != calendar_id:
            raise GcalApiError(404, "Not Found")
        event = self.events[event_id]
        for key, value in body.items():
            if value is None:
                event.pop(key, None)
            else:
                event[key] = copy.deepcopy(value)
        return copy.deepcopy(event)

    def delete_event(self, calendar_id, event_id):
        self.calls.append(("delete", calendar_id, event_id))
        self._maybe_fail("delete")
        if event_id not in self.events or self._calendar_of[event_id] != calendar_id:
            raise GcalApiError(410, "Resource has been deleted")
        del self.events[event_id]

    # -- test helpers ------------------------------------------------------
    def ops(self, name):
        return [c for c in self.calls if c[0] == name]

    def clear_calls(self):
        self.calls.clear()

    def titles(self, day=None):
        """Sorted summaries (optionally only events starting on `day`)."""
        rows = [(e["start"]["dateTime"], e["summary"]) for e in self.events.values()
                if day is None or e["start"]["dateTime"].startswith(day)]
        return [summary for _, summary in sorted(rows)]

    def find(self, appointment_id):
        return [e for e in self.events.values()
                if ((e.get("extendedProperties") or {}).get("private") or {}).get("appointment_id") == str(appointment_id)]

    def external_delete(self, event_id):
        """Someone deletes the event by hand in Google."""
        del self.events[event_id]

    def external_add(self, summary, start, end, private=None, calendar_id="clinic-demo@example.com"):
        """Someone adds an event by hand (no private props unless given)."""
        event = {"id": "evt{}".format(self._next_id), "summary": summary,
                 "start": {"dateTime": start}, "end": {"dateTime": end}}
        if private is not None:
            event["extendedProperties"] = {"private": private}
        self._next_id += 1
        self.events[event["id"]] = event
        self._calendar_of[event["id"]] = calendar_id
        return event["id"]
