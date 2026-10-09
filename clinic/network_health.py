"""Is the internet the reason an answer was poor? A small in-process health monitor.

The app depends on the internet for exactly three things (the browser talks to the app on localhost and the
local model is not network):

    voice      Sarvam speech-to-text           (clinic/realtime_voice.py: the connection attempts)
    planner    Sarvam hosted command planner   (clinic/nlu/sarvam.py: each HTTP attempt)
    whatsapp   WhatsApp / Meta Graph sends     (clinic/whatsapp.py: _post_message)

Each of those call sites reports its outcome with `record(service, ok, ms, kind)`. Only NETWORK evidence counts: a
timeout, a refused / reset / unreachable connection, a TLS handshake that never finished, a connection lost mid-use.
A refused key (HTTP 401/403), an HTTP 429 or 5xx, a model that answered without a tool call, a bad reply and an open
circuit breaker are NOT network failures (`classify` returns None for them and the call sites record nothing, or an
"ok" when an HTTP reply did arrive: a reply proves the network works).

An idle check (`start_probe`, run from the 60 s scheduler tick in a short-lived thread) opens a bare TCP connection
to the two hosts and closes it at once (no TLS, no HTTP, no data) so the state is known before anybody speaks.

The state shown in the sidebar chip, with hysteresis so one failure never flips it:

    GOOD -> SLOW   at least SLOW_FAILURES (2) network failures inside WINDOW_S (300 s), or the newest idle check of
                   a host took longer than PROBE_SLOW_MS (2000 ms)
    any  -> DOWN   DOWN_CONSECUTIVE (3) network failures in a row (across services) inside the window, or every
                   idle-check host failing PROBE_DOWN_ROUNDS (2) checks in a row
    SLOW/DOWN -> GOOD   RECOVER_SUCCESSES (3) successful calls in a row, or no failure for RECOVER_QUIET_S (120 s)
                   with at least one success since, or nothing bad left in the window. On recovery the older
                   evidence is forgotten (`_floor`), so the same old failures cannot push it back to SLOW.
    DOWN stays DOWN until it recovers (it does not drift back to SLOW by itself).

(A browser that reports itself offline shows "No connection" in the page; the server is not told.)

Per service (popover rows): NOT RESPONDING when its last 2 calls failed, SLOW when it has 2 or more failures among
its last SERVICE_RECENT (4) calls, its latest call failed, or its latest idle check was slow, else OK, with
"k of last n failed" next to it whenever any failed.

Everything here is thread-safe and never raises into a call site. Failures (not successes) are also written to the
additive `network_events` table by a sink the app wires up (`app.wire_network_health`), kept to 500 rows / 30 days.
Nothing stores or exposes hosts, URLs, keys or exception text: only a service, a fixed plain wording and a length.
"""

import logging
import os
import socket
import ssl
import threading
import time
from collections import deque, namedtuple
from datetime import datetime, timedelta, timezone

import httpx
from websockets.exceptions import ConnectionClosed

from clinic.timefmt import IST, utc_to_ist

_logger = logging.getLogger(__name__)

# ---- the rules, in one place -------------------------------------------------------------------------------------
WINDOW_S = 300.0              # how long a failure stays evidence
SLOW_FAILURES = 2             # network failures in the window that make it SLOW
DOWN_CONSECUTIVE = 3          # failures in a row (across services) that make it DOWN
PROBE_SLOW_MS = 2000          # an idle check slower than this is SLOW
PROBE_DOWN_ROUNDS = 2         # every idle-check host failing this many checks in a row is DOWN
RECOVER_SUCCESSES = 3         # good calls in a row that bring it back to GOOD
RECOVER_QUIET_S = 120.0       # ...or this long with no failure and at least one success
SERVICE_RECENT = 4            # calls per service the popover looks at
PROBE_TIMEOUT_S = 3.0         # the bare TCP connect of the idle check
KEEP_EVENTS = 300             # memory cap on events kept
KEEP_ROWS = 500               # network_events rows kept
KEEP_DAYS = 30
SHOWN_ROWS = 50

GOOD, SLOW, DOWN = "good", "slow", "down"
STATE_LABELS = {GOOD: "Connection good", SLOW: "Connection slow", DOWN: "No connection"}
SERVICES = ("voice", "planner", "whatsapp")
SERVICE_NAMES = {"voice": "Voice (microphone)", "planner": "Understanding commands", "whatsapp": "WhatsApp messages"}
STATUS_LABELS = {"ok": "OK", "slow": "Slow", "down": "Not responding"}
KINDS = ("timeout", "handshake", "connect", "dropped")
WHAT = {"timeout": "Did not answer in time", "handshake": "Secure connection timed out",
        "connect": "Could not connect", "dropped": "Connection dropped"}
WHAT_UNKNOWN = "Connection problem"

SARVAM_HOST = ("api.sarvam.ai", 443)
META_HOST = ("graph.facebook.com", 443)

Event = namedtuple("Event", "seq t service ok ms kind slow probe services")
ProbeTarget = namedtuple("ProbeTarget", "name host port service services")


# ---- what kind of failure is it? ---------------------------------------------------------------------------------

def classify(exc, phase="call"):
    """The plain kind of a NETWORK failure ("timeout", "handshake", "connect", "dropped"), or None when `exc` is not
    network evidence (a certificate problem, a refused key, a bug). `phase` is "connect" while a connection is
    being opened (any failure there is "could not connect", except a handshake) or "call" for a request in flight.
    Never raises."""
    try:
        return _classify(exc, phase)
    except Exception:
        return None


def _classify(exc, phase):
    text = str(exc).lower()
    connecting = phase == "connect"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return None
    if "handshake" in text and ("time" in text or isinstance(exc, ssl.SSLError)):
        return "handshake"
    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ConnectError):
        return "connect"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "connect" if connecting else "dropped"
    if isinstance(exc, httpx.HTTPError):
        return None                                  # a bad redirect, an undecodable body, ...
    if isinstance(exc, ConnectionClosed):
        return "connect" if connecting else "dropped"
    if isinstance(exc, ssl.SSLError):
        return "connect" if connecting else "dropped"
    if isinstance(exc, (TimeoutError, socket.timeout)) or type(exc).__name__ == "TimeoutError":
        return "connect" if connecting else "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connect"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connect" if connecting else "dropped"
    if isinstance(exc, OSError):
        return "connect"                             # unreachable, no route, name not found, ...
    return None                                      # includes a websocket InvalidStatus (a refused key) and bugs


# ---- the monitor -------------------------------------------------------------------------------------------------

def _empty_signature():
    return (GOOD, tuple(("ok", None) for _ in SERVICES))


class Monitor:
    """Sliding-window health state. `clock` returns epoch seconds. `sink(event_dict)` persists a failure and
    `emit(payload)` broadcasts a change; both are optional, called outside the lock, and may fail silently."""

    def __init__(self, clock=time.time, sink=None, emit=None):
        self._clock = clock
        self.sink, self.emit = sink, emit
        self._lock = threading.RLock()
        self._reset_state()

    def _reset_state(self):
        self._events = deque(maxlen=KEEP_EVENTS)
        self._seq = 0
        self._floor = 0
        self._state = GOOD
        self._last_t = None
        self._last_sig = _empty_signature()

    def reset(self):
        with self._lock:
            self._reset_state()

    # -- recording ----------------------------------------------------------------------------------------------

    def record(self, service, ok, ms=None, kind=None, probe=None, services=None):
        """One outcome of a network call. `ok` False is a network failure of `kind`. `probe` names an idle-check
        host; `services` are the services that host stands for. Never raises."""
        try:
            if service not in SERVICES:
                return
            ok = bool(ok)
            now = self._clock()
            ms = None if ms is None else max(0, int(ms))
            kind = kind if kind in KINDS else None
            covered = tuple(s for s in (services or (service,)) if s in SERVICES) or (service,)
            slow = bool(ok and probe and ms is not None and ms > PROBE_SLOW_MS)
            with self._lock:
                self._seq += 1
                event = Event(self._seq, now, service, ok, ms, kind, slow, probe, covered)
                self._events.append(event)
                self._last_t = now
                payload = self._changed_payload_locked(now)
            if not ok:
                self._persist(event)
            self._broadcast(payload)
        except Exception:
            _logger.debug("network health: could not record an outcome", exc_info=True)

    def _persist(self, event):
        sink = self.sink
        if sink is None:
            return
        try:
            sink({"t": event.t, "service": event.service, "kind": event.kind, "ms": event.ms})
        except Exception:
            _logger.debug("network health: could not store an incident", exc_info=True)

    def _broadcast(self, payload):
        emit = self.emit
        if payload is None or emit is None:
            return
        try:
            emit(payload)
        except Exception:
            _logger.debug("network health: could not broadcast the status", exc_info=True)

    # -- reading ------------------------------------------------------------------------------------------------

    def status(self):
        """The current state and per-service rows (the /network/status payload). Also broadcasts when time alone
        has changed the answer (a failure aged out, a quiet spell ended)."""
        now = self._clock()
        with self._lock:
            payload = self._changed_payload_locked(now, force=True)
        self._broadcast(payload[1])
        return payload[0]

    def refresh(self):
        """Re-evaluate with the passage of time (called every scheduler tick). Never raises."""
        try:
            self.status()
        except Exception:
            _logger.debug("network health: refresh failed", exc_info=True)

    @property
    def state(self):
        return self.status()["state"]

    # -- the state machine ---------------------------------------------------------------------------------------

    def _changed_payload_locked(self, now, force=False):
        """Re-evaluate. Returns the payload to broadcast (state or a service row changed), else None. With
        `force`, returns (payload, broadcast-or-None) so a reader always gets the payload."""
        snapshot = self._evaluate_locked(now)
        signature = (snapshot["state"], tuple((row["status"], row["detail"]) for row in snapshot["services"]))
        changed = signature != self._last_sig
        self._last_sig = signature
        if force:
            return snapshot, (snapshot if changed else None)
        return snapshot if changed else None

    def _evaluate_locked(self, now):
        events = self._events
        while events and now - events[0].t > WINDOW_S:
            events.popleft()
        recent = list(events)
        live = [e for e in recent if e.seq > self._floor]

        failures = [e for e in live if not e.ok]
        run_fail = 0
        for event in reversed(live):
            if event.ok:
                break
            run_fail += 1
        run_ok = 0
        for event in reversed(recent):
            if not event.ok or event.slow:
                break
            run_ok += 1
        bad = [e for e in recent if not e.ok or e.slow]
        last_bad = bad[-1] if bad else None
        success_after_bad = last_bad is not None and any(
            e.ok and not e.slow for e in recent if e.seq > last_bad.seq)

        probes = {}
        for event in live:
            if event.probe:
                probes.setdefault(event.probe, []).append(event)
        probe_down = bool(probes) and all(
            len(found) >= PROBE_DOWN_ROUNDS and all(not e.ok for e in found[-PROBE_DOWN_ROUNDS:])
            for found in probes.values())
        probe_slow = any(found[-1].slow for found in probes.values())

        is_down = run_fail >= DOWN_CONSECUTIVE or probe_down
        is_slow = len(failures) >= SLOW_FAILURES or probe_slow
        recovered = (run_ok >= RECOVER_SUCCESSES or last_bad is None
                     or (now - last_bad.t >= RECOVER_QUIET_S and success_after_bad))

        state = self._state
        if state != GOOD and recovered:
            state = GOOD
            self._floor = self._seq
            live = []
        elif is_down:
            state = DOWN
        elif state == DOWN:
            pass
        elif is_slow:
            state = SLOW
        self._state = state
        return self._snapshot(state, live)

    def _snapshot(self, state, live):
        rows = []
        for service in SERVICES:
            calls = [e for e in live if service in e.services][-SERVICE_RECENT:]
            failed = sum(1 for e in calls if not e.ok)
            if len(calls) >= 2 and not calls[-1].ok and not calls[-2].ok:
                status = "down"
            elif failed >= 2 or (calls and (not calls[-1].ok or calls[-1].slow)):
                status = "slow"
            else:
                status = "ok"
            detail = None
            if failed:
                detail = "{} of last {} failed".format(failed, len(calls)) if len(calls) > 1 else "last call failed"
            rows.append({"key": service, "name": SERVICE_NAMES[service], "status": status,
                         "label": STATUS_LABELS[status], "failed": failed, "total": len(calls), "detail": detail})
        last_t = self._last_t
        return {"state": state, "label": STATE_LABELS[state], "services": rows,
                "last_checked": None if last_t is None else datetime.fromtimestamp(last_t, IST).strftime("%H:%M"),
                "last_checked_ts": last_t}


# ---- the shared monitor and the call-site API ---------------------------------------------------------------------

_monitor = Monitor()


def record(service, ok, ms=None, kind=None, probe=None, services=None):
    _monitor.record(service, ok, ms=ms, kind=kind, probe=probe, services=services)


def status():
    return _monitor.status()


def refresh():
    _monitor.refresh()


def configure(sink=None, emit=None, clock=None):
    """Wire persistence, the broadcast and (tests) the clock into the shared monitor. app.py does this from
    wire_network_health(); importing this module wires nothing, so tests can never reach a real database."""
    _monitor.sink, _monitor.emit = sink, emit
    if clock is not None:
        _monitor._clock = clock


def reset(clock=None, keep_wiring=False):
    """Forget every outcome (tests). The sink and broadcast are cleared too unless `keep_wiring`."""
    _monitor.reset()
    if not keep_wiring:
        _monitor.sink = _monitor.emit = None
    _monitor._clock = clock or time.time


def monitor():
    return _monitor


# ---- the incident log (additive table network_events) ---------------------------------------------------------------

def _utc_text(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write_incident(conn, event, keep_rows=KEEP_ROWS, keep_days=KEEP_DAYS):
    """Insert one failure and prune: at most `keep_rows` rows and nothing older than `keep_days`. `event` is the
    sink dict. The stored wording is fixed text; nothing from the exception itself is kept."""
    kind = event.get("kind") if event.get("kind") in KINDS else None
    conn.execute("INSERT INTO network_events (ts, service, kind, detail, duration_ms) VALUES (?, ?, ?, ?, ?)",
                 (_utc_text(event["t"]), event["service"], kind, WHAT.get(kind), event.get("ms")))
    conn.execute("DELETE FROM network_events WHERE ts < ?", (_utc_text(event["t"] - keep_days * 86400),))
    conn.execute("DELETE FROM network_events WHERE id <= "
                 "(SELECT id FROM network_events ORDER BY id DESC LIMIT 1 OFFSET ?)", (keep_rows,))
    conn.commit()


def make_db_sink(connect, background=False):
    """A sink for Monitor that writes through `connect()` (a fresh connection per incident: the callers are
    arbitrary threads). With `background` the write runs in its own short thread so a locked database can never
    hold up the call that failed. Errors are swallowed."""
    def write(event):
        conn = None
        try:
            conn = connect()
            write_incident(conn, event)
        except Exception:
            _logger.debug("network health: could not write an incident", exc_info=True)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def sink(event):
        if background:
            threading.Thread(target=write, args=(event,), name="network-incident", daemon=True).start()
        else:
            write(event)
    return sink


def incidents(conn, now=None, current=None):
    """The Connection tab: {today_count, state, label, rows (newest first, last SHOWN_ROWS)}. A day is the clinic's
    IST day. Times are IST, like the other tables. Never raises on a missing table."""
    now = _monitor._clock() if now is None else now
    current = current or _monitor.status()
    day_start = datetime.fromtimestamp(now, IST).replace(hour=0, minute=0, second=0, microsecond=0)
    out = {"today_count": 0, "state": current["state"], "label": current["label"], "rows": []}
    try:
        out["today_count"] = conn.execute("SELECT COUNT(*) FROM network_events WHERE ts >= ?",
                                          (day_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0]
        found = conn.execute("SELECT ts, service, kind, detail, duration_ms FROM network_events "
                             "ORDER BY id DESC LIMIT ?", (SHOWN_ROWS,)).fetchall()
    except Exception:
        _logger.debug("network health: could not read the incidents", exc_info=True)
        return out
    for row in found:
        seconds = None if row["duration_ms"] is None else round(row["duration_ms"] / 1000.0, 1)
        out["rows"].append({
            "when": utc_to_ist(row["ts"]),
            "service": SERVICE_NAMES.get(row["service"], row["service"]),
            "what": WHAT.get(row["kind"]) or row["detail"] or WHAT_UNKNOWN,
            "seconds": seconds,
            "length": "-" if seconds is None else "{:.1f} s".format(seconds),
        })
    return out


# ---- the idle check -------------------------------------------------------------------------------------------------

def probe_enabled():
    """NETWORK_PROBE_ENABLED=0 switches the idle check off (tests do)."""
    return os.environ.get("NETWORK_PROBE_ENABLED", "1").strip() != "0"


def probe_targets():
    """The hosts worth checking right now: Sarvam when a key is set, Meta only when WhatsApp sends for real."""
    from clinic import notify          # late: notify imports whatsapp, which imports this module
    targets = []
    if os.environ.get("SARVAM_API_KEY", "").strip():
        targets.append(ProbeTarget("sarvam", SARVAM_HOST[0], SARVAM_HOST[1], "planner", ("voice", "planner")))
    if notify.notify_mode() == "live" and os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip():
        targets.append(ProbeTarget("meta", META_HOST[0], META_HOST[1], "whatsapp", ("whatsapp",)))
    return targets


def probe_once(targets=None, connect=None, clock=time.monotonic, target_monitor=None, timeout=PROBE_TIMEOUT_S):
    """Open a bare TCP connection to each target and close it at once (no TLS, no HTTP, nothing sent), feeding the
    connect time or failure to the monitor. Returns [(name, ok, ms)]."""
    target_monitor = target_monitor or _monitor
    connect = connect or socket.create_connection
    results = []
    for target in (probe_targets() if targets is None else targets):
        started = clock()
        try:
            sock = connect((target.host, target.port), timeout=timeout)
        except Exception as exc:
            ms = int((clock() - started) * 1000)
            target_monitor.record(target.service, False, ms, classify(exc, "connect") or "connect",
                                  probe=target.name, services=target.services)
            results.append((target.name, False, ms))
            continue
        ms = int((clock() - started) * 1000)
        try:
            sock.close()
        except Exception:
            pass
        target_monitor.record(target.service, True, ms, probe=target.name, services=target.services)
        results.append((target.name, True, ms))
    return results


_probe_running = threading.Lock()


def start_probe(probe=None):
    """Run the idle check in a short-lived daemon thread and return at once, so a slow network can never delay the
    scheduler tick. `probe` is a callable (tests, or a custom host list); None runs the real check, and only when
    NETWORK_PROBE_ENABLED allows. At most one check runs at a time. Returns the thread, or None when nothing
    was started."""
    if probe is None:
        if not probe_enabled():
            return None
        probe = probe_once
    if not _probe_running.acquire(False):
        return None

    def run():
        try:
            probe()
        except Exception:
            _logger.debug("network health: idle check failed", exc_info=True)
        finally:
            _probe_running.release()

    thread = threading.Thread(target=run, name="network-probe", daemon=True)
    try:
        thread.start()
    except Exception:
        _probe_running.release()
        return None
    return thread
