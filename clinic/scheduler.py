"""Background notification scheduler: a lightweight in-process daemon
thread that, about once a minute,

  (a) delivers pending notifications, retrying recently failed ones a
      bounded number of times (see notify.RETRY_MAX_ATTEMPTS), and
  (b) generates the day-before and morning reminders
      (notify.generate_reminders; hour constants live in clinic/notify.py), plus
      the two follow-up reminders (followups.tick: any whose time has passed
      while the visit is still ahead is sent now, so a tick after downtime
      catches up), and
  (c) when Google Calendar sync is configured, drains the calendar sync queue
      (bounded retries) and queues a full reconcile of today..+30 days about
      every 10 minutes (clinic/gcal_sync.py). This step runs LAST and is
      best-effort, so a slow or failing Google can never hold up patient
      notifications.

Design points:
  * start() is called from app.py's __main__ block only -- never at import
    time, and never by tests. Tests call tick() directly with an injected
    clock and a fake sender; no thread and no sleeping is involved.
  * Every step of a tick catches and logs its own exceptions, and the loop
    catches around the whole tick, so a bad row or a locked database can
    never kill the thread.
  * Reminder generation is idempotent (dedup keys), so overlapping or
    repeated ticks never duplicate a message.
"""

import logging
import threading

from clinic import followups, gcal_client, gcal_sync, notify

_logger = logging.getLogger(__name__)

TICK_SECONDS = 60


def tick(conn, sender=None, dry_run=False, now=None, calendar_client=None):
    """One scheduler pass. `sender`/`dry_run` as for notify.flush; `now` is a
    notify.Now (both clocks), injectable for tests. `calendar_client` is the
    Google Calendar client (a fake in tests); None means calendar sync is not
    configured and that step is skipped. Returns a small summary dict; never
    raises."""
    now = now or notify.Now.real()
    summary = {"reminders_created": 0, "followup_reminders": 0, "flushed": {}, "errors": 0, "calendar": None}

    def step(name, fn):
        try:
            return fn()
        except Exception:
            summary["errors"] += 1
            _logger.exception("scheduler step %r failed", name)
            try:
                conn.rollback()
            except Exception:
                pass
            return None

    # Deliver whatever is already waiting (including retries) first, so a
    # slow reminder-generation step can't delay it...
    flushed = step("flush", lambda: notify.flush(conn, sender, now=now, dry_run=dry_run))
    created = step("reminders", lambda: notify.generate_reminders(conn, now))
    summary["reminders_created"] = created or 0
    followup_created = step("followup-reminders", lambda: followups.tick(conn, now))
    summary["followup_reminders"] = followup_created or 0
    # ...then send the reminders just created without waiting a full minute.
    if created or followup_created:
        again = step("flush-reminders", lambda: notify.flush(conn, sender, now=now, dry_run=dry_run))
        for status, count in (again or {}).items():
            flushed = flushed or {}
            flushed[status] = flushed.get(status, 0) + count
    summary["flushed"] = flushed or {}
    # Google Calendar sync last: it makes network calls that may be slow.
    if calendar_client is not None:
        summary["calendar"] = step("calendar", lambda: gcal_sync.tick(conn, calendar_client, now=now))
    return summary


def start(get_conn, sender_override=None, interval=TICK_SECONDS, stop_event=None, calendar_client=None):
    """Start the daemon thread and return it. `get_conn()` must return a
    fresh sqlite connection (one per tick: connections are not shared
    across threads). `sender_override()` may return a sender to use instead
    of the one WHATSAPP_NOTIFY_MODE selects (tests/dev only).
    `calendar_client()` returns the Google Calendar client for this tick (None
    when sync is not configured); the default builds the real one lazily and
    only when configured. The environment is read at app start (load_dotenv),
    so newly added credentials need an app restart."""
    client_for_tick = calendar_client or gcal_client.get_client
    stop_event = stop_event or threading.Event()

    def loop():
        _logger.info("notification scheduler started (every %ss, mode=%s)", interval, notify.notify_mode())
        while not stop_event.is_set():
            conn = None
            try:
                conn = get_conn()
                override = sender_override() if sender_override else None
                sender, dry_run = notify.resolve_sender(override)
                try:
                    client = client_for_tick()
                except Exception:
                    _logger.exception("could not build the Google Calendar client")
                    client = None
                tick(conn, sender, dry_run, calendar_client=client)
            except Exception:
                _logger.exception("scheduler tick failed")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            stop_event.wait(interval)

    thread = threading.Thread(target=loop, name="notification-scheduler", daemon=True)
    thread.stop_event = stop_event
    thread.start()
    return thread
