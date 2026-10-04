"""One-way sync of appointments INTO a Google Calendar.

SQLite stays the source of truth and staff approval of every write is
unchanged; this module only mirrors the result into a calendar. Edits made
directly in Google never flow back (and are healed back to the database's
version by the periodic reconcile).

The core primitive is reconcile-by-date
---------------------------------------
`resync_date(conn, client, date)` makes Google match the database for ONE
day: it lists the events this app created for that day, then

  * creates an event for every appointment that has none (including one that
    was deleted by hand in Google, and an event Google no longer knows when we
    try to patch it -- 404/410),
  * patches an event whose title / time / colour is out of date,
  * deletes the event of a cancelled / rescheduled appointment, any duplicate,
    and any leftover of an appointment that moved to another day (404/410 on
    delete is success).

It is idempotent: with nothing to change it makes exactly one (read-only)
list call. A token is a rank among the day's active appointments
(clinic/token_queue.py), so one booking or cancellation renumbers the people
behind it -- reconciling the whole day is what retitles THEIR events too.

How work gets triggered
-----------------------
  * after an approved write (clinic/notify's neighbour in app.py):
    `post_commit()` only INSERTs a row into `calendar_sync_queue` -- it can
    never raise, never calls Google itself and never delays the response; a
    background thread (or the scheduler tick) drains the queue;
  * the scheduler tick (`tick()`): drains the queue with bounded retries and
    enqueues a full reconcile of today..today+30d every ~10 minutes, healing
    drift and earlier failures;
  * the "Sync now" button: enqueues a full reconcile.

If the integration is not configured nothing is enqueued and every entry
point is a clean no-op.

Privacy: an event carries only `T-04 . Sunita D.` and the time. No phone,
age, reason or notes ever leave this database.
"""

import logging
import threading
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from clinic import gcal_config, scheduling, token_queue
from clinic.gcal_client import GcalApiError
from clinic.notify import Now

_logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

WINDOW_DAYS = 30                 # a full reconcile covers today .. today + WINDOW_DAYS
PERIODIC_FULL_MINUTES = 10       # how often the scheduler enqueues a full reconcile
MAX_ATTEMPTS = 5                 # a queue row that keeps failing is given up on (the periodic reconcile still heals it)
DRAIN_MAX_ROUNDS = 5             # batches per drain() call
DRAIN_BATCH = 100
PRUNE_AFTER_DAYS = 2             # finished queue rows are kept this long for inspection
DEFAULT_DURATION_MINUTES = scheduling.SLOT_MINUTES

# Google's palette ids (see the Calendar API "colors" docs): graphite / tomato.
STATUS_COLOR_ID = {"completed": "8", "no_show": "11"}

# Intents whose commit can change what the calendar should show. Check-in and
# "call next" only move queue_state, which the calendar does not display.
CALENDAR_INTENTS = frozenset((
    "book_appointment", "cancel_appointment", "reschedule_appointment", "restore_appointment",
    "queue_mark_done", "queue_mark_no_show",
))

_DRAIN_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(text):
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_rfc3339(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _day_bounds(day):
    """RFC 3339 [start, end) of an ISO date in IST."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _dates(start_date, end_date):
    d0 = date_cls.fromisoformat(start_date)
    d1 = date_cls.fromisoformat(end_date)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _event_day(event):
    """The IST date an event starts on, or None (all-day / unparsable)."""
    raw = (event.get("start") or {}).get("dateTime")
    if not raw:
        return None
    try:
        return _parse_rfc3339(raw).astimezone(IST).date().isoformat()
    except ValueError:
        return None


def _instant(slot):
    raw = (slot or {}).get("dateTime")
    if not raw:
        return None
    try:
        return _parse_rfc3339(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Event payload (minimal by design)
# ---------------------------------------------------------------------------

def display_name(name):
    """'Sunita Devi' -> 'Sunita D.'; a single word stays as is; nothing at all
    -> 'Patient'. Only a first name and one initial ever leave the clinic."""
    parts = (name or "").split()
    if not parts:
        return "Patient"
    first = parts[0][:30]
    if len(parts) == 1:
        return first
    return "{} {}.".format(first, parts[-1][0].upper())


def event_title(token_label, name):
    return "{} · {}".format(token_label, display_name(name))


def _start_end(appt_date, start_time, duration_minutes):
    clock = start_time.strip()
    fmt = "%H:%M:%S" if clock.count(":") == 2 else "%H:%M"
    start = datetime.strptime("{} {}".format(appt_date, clock), "%Y-%m-%d " + fmt).replace(tzinfo=IST)
    end = start + timedelta(minutes=duration_minutes or DEFAULT_DURATION_MINUTES)
    return start, end


def build_event_body(entry):
    """The Google event for one token_queue.day_queue() entry. `extended
    private` carries our appointment id (the mapping) and a marker saying the
    event is ours; nothing else about the patient is included."""
    start, end = _start_end(entry["appt_date"], entry["start_time"], entry["duration_minutes"])
    body = {
        "summary": event_title(entry["token_label"], entry["name"]),
        "start": {"dateTime": start.isoformat(), "timeZone": gcal_config.TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": gcal_config.TIMEZONE},
        "extendedProperties": {"private": {
            gcal_config.SOURCE_PROPERTY: gcal_config.SOURCE_VALUE,
            gcal_config.APPOINTMENT_PROPERTY: str(entry["id"]),
        }},
    }
    color = STATUS_COLOR_ID.get(entry["status"])
    if color:
        body["colorId"] = color
    return body


def _patch_body(body):
    """A PATCH body: the visible fields only, with an explicit null colorId so
    a status that no longer has a colour clears it."""
    return {
        "summary": body["summary"], "start": body["start"], "end": body["end"],
        "colorId": body.get("colorId"),
    }


def _differs(existing, body):
    if existing.get("summary") != body["summary"]:
        return True
    if _instant(existing.get("start")) != _instant(body["start"]):
        return True
    if _instant(existing.get("end")) != _instant(body["end"]):
        return True
    return existing.get("colorId") != body.get("colorId")


# ---------------------------------------------------------------------------
# State + queue (tables are in clinic/schema.sql; strictly additive)
# ---------------------------------------------------------------------------

def get_state(conn, key):
    row = conn.execute("SELECT value FROM calendar_sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key, value, now=None):
    now = now or Now.real()
    conn.execute(
        "INSERT INTO calendar_sync_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _ts(now.utc)),
    )
    conn.commit()


def enqueue(conn, kind, target="", now=None):
    """Queue one piece of sync work. `kind`: 'date' (target = YYYY-MM-DD),
    'appointment' (target = appointment id; its current day AND the day its
    event is on are reconciled, which covers a reschedule) or 'full'.
    Returns the new row id, or None if an identical row is already pending."""
    now = now or Now.real()
    if kind not in ("date", "appointment", "full"):
        raise ValueError("unknown calendar sync kind: {}".format(kind))
    if conn.execute(
        "SELECT 1 FROM calendar_sync_queue WHERE kind = ? AND target = ? AND status = 'pending'", (kind, str(target))
    ).fetchone():
        return None
    cur = conn.execute(
        "INSERT INTO calendar_sync_queue (kind, target, status, attempts, created_at, updated_at) "
        "VALUES (?, ?, 'pending', 0, ?, ?)",
        (kind, str(target), _ts(now.utc), _ts(now.utc)),
    )
    conn.commit()
    return cur.lastrowid


def post_commit(conn, intent, slots, entity_id, *, kick=None, now=None):
    """The ONE call the web layer makes after a write has committed. It only
    records that a day needs reconciling (a single INSERT) and pokes `kick`
    (which must itself just hand the work to another thread). It can never
    raise: a calendar problem must not fail, delay or mask a write that
    already succeeded."""
    try:
        if intent not in CALENDAR_INTENTS or not gcal_config.is_configured():
            return
        appointment_id = _to_int(entity_id if intent == "book_appointment" else (slots or {}).get("appointment_id"))
        if appointment_id is None:
            return
        enqueue(conn, "appointment", appointment_id, now)
        if kick is not None:
            kick()
    except Exception:
        _logger.exception("calendar sync hook failed for intent=%s (the write itself succeeded)", intent)
        try:
            conn.rollback()
        except Exception:
            pass


def request_full_sync(conn, kick=None, now=None):
    """The "Sync now" button: queue a full reconcile and return immediately."""
    enqueue(conn, "full", "", now)
    if kick is not None:
        try:
            kick()
        except Exception:
            _logger.exception("could not start the calendar sync worker")


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def _first_error(result):
    return result["errors"][0] if result["errors"] else None


def _new_result(day):
    return {"date": day, "created": 0, "patched": 0, "deleted": 0, "unchanged": 0, "errors": []}


def _desired_events(conn, day, errors):
    """{appointment_id: event body} for the day's non-cancelled, non-rescheduled
    appointments. One bad row (an unreadable time) is reported, not fatal."""
    desired = {}
    for entry in token_queue.day_queue(conn, day):
        try:
            desired[entry["id"]] = build_event_body(entry)
        except Exception as exc:
            errors.append("appointment #{}: cannot build event ({})".format(entry["id"], exc))
    return desired


def _mapping_rows(conn, calendar_id, day, appointment_ids):
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE calendar_id = ? AND appt_date = ?", (calendar_id, day)
    ).fetchall()
    found = {row["appointment_id"]: row for row in rows}
    ids = [i for i in appointment_ids if i not in found]
    if ids:
        marks = ",".join("?" for _ in ids)
        for row in conn.execute(
            "SELECT * FROM calendar_events WHERE calendar_id = ? AND appointment_id IN ({})".format(marks),
            [calendar_id] + ids,
        ).fetchall():
            found[row["appointment_id"]] = row
    return found


def _save_mapping(conn, appointment_id, event_id, calendar_id, day, now):
    conn.execute(
        "INSERT INTO calendar_events (appointment_id, gcal_event_id, calendar_id, appt_date, last_synced_at, last_error, status) "
        "VALUES (?, ?, ?, ?, ?, NULL, 'synced') "
        "ON CONFLICT(appointment_id) DO UPDATE SET gcal_event_id = excluded.gcal_event_id, "
        "calendar_id = excluded.calendar_id, appt_date = excluded.appt_date, "
        "last_synced_at = excluded.last_synced_at, last_error = NULL, status = 'synced'",
        (appointment_id, event_id, calendar_id, day, _ts(now.utc)),
    )
    conn.commit()


def _drop_mapping(conn, appointment_id, event_id=None):
    if event_id is None:
        conn.execute("DELETE FROM calendar_events WHERE appointment_id = ?", (appointment_id,))
    else:
        conn.execute(
            "DELETE FROM calendar_events WHERE appointment_id = ? AND gcal_event_id = ?", (appointment_id, event_id))
    conn.commit()


def _note_error(conn, appointment_id, message):
    conn.execute(
        "UPDATE calendar_events SET last_error = ?, status = 'error' WHERE appointment_id = ?",
        (message[:300], appointment_id),
    )
    conn.commit()


def _delete_event(client, calendar_id, event_id):
    """Delete, treating 'already gone' as success."""
    try:
        client.delete_event(calendar_id, event_id)
    except GcalApiError as exc:
        if not exc.is_gone:
            raise


def _reconcile_date(conn, client, calendar_id, day, events, now):
    """Make Google match the database for `day`, given the app-created events
    Google currently shows for that day. Per-event failures are collected in
    result["errors"] and never stop the rest of the day."""
    result = _new_result(day)
    desired = _desired_events(conn, day, result["errors"])
    mapped = _mapping_rows(conn, calendar_id, day, list(desired))

    by_appointment = defaultdict(list)
    for event in events:
        props = (event.get("extendedProperties") or {}).get("private") or {}
        if props.get(gcal_config.SOURCE_PROPERTY) != gcal_config.SOURCE_VALUE:
            continue  # not ours: never touched
        if _event_day(event) != day:
            continue  # starts on another day (e.g. spans midnight): that day owns it
        by_appointment[_to_int(props.get(gcal_config.APPOINTMENT_PROPERTY))].append(event)
    seen_event_ids = {e["id"] for group in by_appointment.values() for e in group}

    def attempt(appointment_id, what, fn):
        try:
            fn()
            return True
        except Exception as exc:
            message = "{} appointment #{}: {}".format(what, appointment_id, exc)
            _logger.warning("calendar sync: %s", message)
            result["errors"].append(message)
            if appointment_id in mapped:
                try:
                    _note_error(conn, appointment_id, message)
                except Exception:
                    conn.rollback()
            return False

    def create(appointment_id, body):
        created = client.insert_event(calendar_id, body)
        _save_mapping(conn, appointment_id, created["id"], calendar_id, day, now)
        result["created"] += 1

    def patch(appointment_id, event_id, body):
        try:
            client.patch_event(calendar_id, event_id, _patch_body(body))
        except GcalApiError as exc:
            if not exc.is_gone:
                raise
            create(appointment_id, body)  # the event vanished under us: recreate it
            return
        _save_mapping(conn, appointment_id, event_id, calendar_id, day, now)
        result["patched"] += 1

    deleted_event_ids = set()

    def remove(appointment_id, event_id):
        _delete_event(client, calendar_id, event_id)
        deleted_event_ids.add(event_id)
        _drop_mapping(conn, appointment_id, event_id)
        result["deleted"] += 1

    # 1. Every appointment that should be on the calendar.
    for appointment_id in sorted(desired):
        body = desired[appointment_id]
        mapping = mapped.get(appointment_id)
        candidates = by_appointment.pop(appointment_id, [])
        if candidates:
            keep = next((e for e in candidates if mapping and e["id"] == mapping["gcal_event_id"]), candidates[0])
            for extra in candidates:
                if extra is not keep:  # a duplicate (e.g. a crash between insert and bookkeeping)
                    attempt(appointment_id, "delete duplicate of", lambda e=extra: remove(appointment_id, e["id"]))
            if _differs(keep, body):
                attempt(appointment_id, "update", lambda: patch(appointment_id, keep["id"], body))
            else:
                try:
                    _save_mapping(conn, appointment_id, keep["id"], calendar_id, day, now)
                    result["unchanged"] += 1
                except Exception:
                    conn.rollback()
        elif mapping is not None and mapping["appt_date"] != day:
            # Rescheduled here from another day: move its existing event.
            attempt(appointment_id, "move", lambda: patch(appointment_id, mapping["gcal_event_id"], body))
        else:
            # No event: new booking, or someone deleted it in Google. (The
            # mapping row, if any, is simply overwritten.)
            attempt(appointment_id, "create", lambda: create(appointment_id, body))

    # 2. Events for appointments that no longer belong on this day: cancelled,
    #    rescheduled away, deleted, or an orphan whose mapping was lost.
    for appointment_id, group in by_appointment.items():
        if appointment_id is None:
            continue  # carries our marker but no usable id: leave it alone
        for event in group:
            attempt(appointment_id, "delete", lambda e=event: remove(appointment_id, e["id"]))

    # 3. Stale mappings for this day whose event Google did not list (already
    #    deleted by hand, or listing lag): clear the event if it still exists.
    for appointment_id, mapping in mapped.items():
        if mapping["appt_date"] != day or appointment_id in desired:
            continue
        event_id = mapping["gcal_event_id"]
        if event_id in seen_event_ids or event_id in deleted_event_ids:
            continue
        attempt(appointment_id, "clear", lambda m=mapping: remove(appointment_id, m["gcal_event_id"]))
    return result


def resync_date(conn, client, day, calendar_id=None, now=None):
    """Reconcile one day. Returns a result dict ({'created', 'patched',
    'deleted', 'unchanged', 'errors': [...]}). Raises GcalApiError only if the
    day's events cannot be listed at all (calendar unreachable / not shared)."""
    now = now or Now.real()
    calendar_id = calendar_id or gcal_config.calendar_id()
    time_min, time_max = _day_bounds(day)
    events = client.list_events(calendar_id, time_min, time_max)
    return _reconcile_date(conn, client, calendar_id, day, events, now)


def resync_range(conn, client, start_date, end_date, calendar_id=None, now=None):
    """Reconcile every day in [start_date, end_date] with ONE list call.
    Returns {date: result}. Raises GcalApiError if the list fails."""
    now = now or Now.real()
    calendar_id = calendar_id or gcal_config.calendar_id()
    time_min = _day_bounds(start_date)[0]
    time_max = _day_bounds(end_date)[1]
    events = client.list_events(calendar_id, time_min, time_max)
    by_day = defaultdict(list)
    for event in events:
        day = _event_day(event)
        if day:
            by_day[day].append(event)
    return {day: _reconcile_date(conn, client, calendar_id, day, by_day.get(day, []), now)
            for day in _dates(start_date, end_date)}


# ---------------------------------------------------------------------------
# Draining the queue
# ---------------------------------------------------------------------------

def _row_dates(conn, row, window):
    kind, target = row["kind"], row["target"]
    if kind == "full":
        return set(window)
    if kind == "date":
        return {target}
    appointment_id = _to_int(target)
    days = set()
    if appointment_id is not None:
        current = conn.execute("SELECT appt_date FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        if current:
            days.add(current["appt_date"])
        mapped = conn.execute(
            "SELECT appt_date FROM calendar_events WHERE appointment_id = ?", (appointment_id,)).fetchone()
        if mapped:
            days.add(mapped["appt_date"])
    return days


def _error_text(exc):
    return " ".join(str(exc).split())[:300] or type(exc).__name__


def _process_rows(conn, client, calendar_id, rows, now):
    """Run one batch of claimed queue rows. Returns (done, retried, gave_up,
    errors, fatal): `fatal` means Google could not be listed at all, so the
    caller should stop for now."""
    window = _dates(now.today.isoformat(), (now.today + timedelta(days=WINDOW_DAYS)).isoformat())
    needed = {row["id"]: _row_dates(conn, row, window) for row in rows}
    results = {}   # day -> None (ok) or an error message
    fatal = None

    if any(row["kind"] == "full" for row in rows):
        try:
            for day, res in resync_range(conn, client, window[0], window[-1], calendar_id, now).items():
                results[day] = _first_error(res)
        except Exception as exc:
            message = _error_text(exc)
            for day in window:
                results[day] = message
            if isinstance(exc, GcalApiError):
                fatal = message  # Google itself is unreachable / refusing: stop for now

    for day in sorted(set().union(*needed.values()) - set(results)):
        if fatal:
            break
        try:
            results[day] = _first_error(resync_date(conn, client, day, calendar_id, now))
        except Exception as exc:
            results[day] = _error_text(exc)
            if isinstance(exc, GcalApiError):
                fatal = results[day]

    stamp = _ts(now.utc)
    done = retried = gave_up = 0
    errors = []
    for row in rows:
        days = needed[row["id"]]
        if any(day not in results for day in days):
            # Never reached (we stopped early): back to pending, no attempt charged.
            conn.execute("UPDATE calendar_sync_queue SET status = 'pending', updated_at = ? WHERE id = ?", (stamp, row["id"]))
            continue
        problem = next((results[day] for day in sorted(days) if results[day]), None)
        if problem is None:
            conn.execute(
                "UPDATE calendar_sync_queue SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
                (stamp, row["id"]))
            done += 1
            continue
        attempts = row["attempts"] + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        conn.execute(
            "UPDATE calendar_sync_queue SET status = ?, attempts = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, attempts, problem, stamp, row["id"]))
        errors.append(problem)
        if status == "failed":
            gave_up += 1
        else:
            retried += 1
    conn.commit()
    return done, retried, gave_up, errors, fatal


def drain(conn, client, now=None, calendar_id=None):
    """Process pending queue rows with `client`. Safe to call from any thread
    (one drain at a time per process; a concurrent call returns at once).
    Never raises. Each row is attempted at most once per call, so a failing
    row is retried on the NEXT call, up to MAX_ATTEMPTS."""
    summary = {"rows": 0, "done": 0, "retry": 0, "failed": 0, "errors": [], "skipped": None}
    if client is None:
        summary["skipped"] = "not configured"
        return summary
    if not _DRAIN_LOCK.acquire(blocking=False):
        summary["skipped"] = "busy"
        return summary
    try:
        now = now or Now.real()
        calendar_id = calendar_id or gcal_config.calendar_id()
        # Claims left over from a crashed worker (we hold the process lock, so
        # nothing is legitimately in flight).
        conn.execute("UPDATE calendar_sync_queue SET status = 'pending' WHERE status = 'processing'")
        conn.commit()
        last_id = 0
        for _ in range(DRAIN_MAX_ROUNDS):
            rows = conn.execute(
                "SELECT * FROM calendar_sync_queue WHERE status = 'pending' AND id > ? ORDER BY id LIMIT ?",
                (last_id, DRAIN_BATCH),
            ).fetchall()
            if not rows:
                break
            last_id = rows[-1]["id"]
            marks = ",".join("?" for _ in rows)
            conn.execute(
                "UPDATE calendar_sync_queue SET status = 'processing' WHERE id IN ({})".format(marks),
                [r["id"] for r in rows])
            conn.commit()
            summary["rows"] += len(rows)
            try:
                done, retried, gave_up, errors, fatal = _process_rows(conn, client, calendar_id, rows, now)
            except Exception as exc:  # belt and braces: _process_rows catches per-day failures itself
                _logger.exception("calendar sync batch failed")
                conn.rollback()
                conn.execute(
                    "UPDATE calendar_sync_queue SET status = 'pending' WHERE status = 'processing'")
                conn.commit()
                summary["errors"].append(_error_text(exc))
                break
            summary["done"] += done
            summary["retry"] += retried
            summary["failed"] += gave_up
            summary["errors"].extend(errors)
            if fatal:
                break
        _record_outcome(conn, summary, now)
    except Exception:
        _logger.exception("calendar sync drain failed")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _DRAIN_LOCK.release()
    return summary


def _record_outcome(conn, summary, now):
    if summary["done"]:
        set_state(conn, "last_success_at", _ts(now.utc), now)
    if summary["errors"]:
        set_state(conn, "last_error", summary["errors"][0], now)
        set_state(conn, "last_error_at", _ts(now.utc), now)
    elif summary["done"]:
        # A clean pass: whatever failed before has healed.
        conn.execute("DELETE FROM calendar_sync_state WHERE key IN ('last_error', 'last_error_at')")
        conn.commit()


def ensure_periodic_full(conn, now):
    """Enqueue a full reconcile if the last one was queued >= PERIODIC_FULL_MINUTES ago."""
    last = get_state(conn, "last_full_enqueued_at")
    if last is not None and now.utc - _parse_ts(last) < timedelta(minutes=PERIODIC_FULL_MINUTES):
        return False
    set_state(conn, "last_full_enqueued_at", _ts(now.utc), now)
    enqueue(conn, "full", "", now)
    return True


def prune(conn, now):
    cutoff = _ts(now.utc - timedelta(days=PRUNE_AFTER_DAYS))
    conn.execute(
        "DELETE FROM calendar_sync_queue WHERE status IN ('done', 'failed') AND updated_at < ?", (cutoff,))
    conn.commit()


def tick(conn, client, now=None):
    """The scheduler's calendar step: schedule the periodic full reconcile,
    drain the queue, tidy up. `client` None (not configured) is a no-op. Never
    raises."""
    summary = {"configured": client is not None, "enqueued_full": False, "drain": None}
    if client is None:
        return summary
    try:
        now = now or Now.real()
        summary["enqueued_full"] = ensure_periodic_full(conn, now)
        summary["drain"] = drain(conn, client, now)
        prune(conn, now)
    except Exception:
        _logger.exception("calendar sync tick failed")
        try:
            conn.rollback()
        except Exception:
            pass
    return summary


# ---------------------------------------------------------------------------
# Status (for the Appointments tab)
# ---------------------------------------------------------------------------

def _local_label(utc_text):
    if not utc_text:
        return None
    return (_parse_ts(utc_text).replace(tzinfo=timezone.utc).astimezone(IST)).strftime("%Y-%m-%d %H:%M")


def sync_status(conn):
    """{'configured', 'calendar_id', 'last_sync_at' (UTC), 'last_sync_local'
    (IST, 'YYYY-MM-DD HH:MM'), 'pending', 'failed', 'last_error'}."""
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM calendar_sync_queue WHERE status IN ('pending', 'processing')").fetchone()["n"]
    failed = conn.execute("SELECT COUNT(*) AS n FROM calendar_sync_queue WHERE status = 'failed'").fetchone()["n"]
    last_ok = get_state(conn, "last_success_at")
    return {
        "configured": gcal_config.is_configured(),
        "calendar_id": gcal_config.calendar_id(),
        "last_sync_at": last_ok,
        "last_sync_local": _local_label(last_ok),
        "pending": pending,
        "failed": failed,
        "last_error": get_state(conn, "last_error"),
    }
