// After an /approve commits a write (voice or WhatsApp), several bits of
// server-rendered data can go stale: the stat row, the Patients table (a
// new patient may have just been registered), Missed follow-ups,
// Attendance, the Audit log, the patient/staff dropdown lists ReviewCard
// draws from, and the Patient messages inbox (the approved item drops out
// of it). The old dashboard just did location.reload() to pick all of this
// up -- but that wipes the Assistant tab's in-memory conversation feed,
// which is the entire point of this redesign. Instead we re-fetch this
// same "/" route the page itself came from (no new backend endpoint) and
// lift the pieces we need out of the fresh HTML. (The Queue tab has its own
// lighter refreshQueue() below.)
window.DashboardRefresh = (function () {
  function copyContent(freshDoc, id) {
    var fresh = freshDoc.getElementById(id);
    var current = document.getElementById(id);
    if (fresh && current) current.innerHTML = fresh.innerHTML;
  }

  function refresh() {
    return fetch(window.location.pathname || "/", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");

        copyContent(doc, "stat-row");
        copyContent(doc, "citation-line");
        copyContent(doc, "patients-table-card");
        copyContent(doc, "missed-followups-card");
        copyContent(doc, "attendance-card");
        copyContent(doc, "audit-table-card");
        // The Queue panel follows the date picker (which may not be today),
        // so it is re-fetched for that date rather than copied from "/".
        refreshQueue();

        var patientsData = doc.getElementById("patients-data");
        var staffData = doc.getElementById("staff-data");
        if (patientsData && staffData && window.ReviewCard) {
          ReviewCard.setData(
            JSON.parse(patientsData.textContent || "[]"),
            JSON.parse(staffData.textContent || "[]")
          );
        }

        var waInboxData = doc.getElementById("wa-inbox-data");
        if (waInboxData && window.WaInbox) {
          window.WaInbox.render(JSON.parse(waInboxData.textContent || "[]"));
        }

        // Conversation threads keep their open/closed state and any
        // half-typed reply across this re-render (see static/wa_threads.js).
        var waThreadsData = doc.getElementById("wa-threads-data");
        if (waThreadsData && window.WaThreads) {
          window.WaThreads.render(JSON.parse(waThreadsData.textContent || "[]"));
        }
      })
      .catch(function () {
        // Best-effort: a failed refresh leaves slightly stale numbers on
        // screen, which is far less harmful than losing the conversation
        // feed to a reload would be.
      });
  }

  // Lightweight refresh of just the Queue tab, from the /queue/partial
  // fragment route (not the whole dashboard): safe to call every ~15s
  // because it can't disturb the Patient messages inbox or the Assistant
  // conversation feed. See static/queue.js, the caller.
  var queueRequest = 0;   // only the newest request may draw (a slower, older one must not overwrite it)

  function refreshQueue() {
    var picker = document.getElementById("queue-date");
    var mine = ++queueRequest;
    var query = [];
    if (picker && picker.value) query.push("date=" + encodeURIComponent(picker.value));
    if (window.Branches) query.push(Branches.viewQuery());
    var url = "/queue/partial" + (query.length ? "?" + query.join("&") : "");
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        if (mine !== queueRequest) return;
        var panel = document.getElementById("queue-panel");
        if (panel) panel.innerHTML = html;
      })
      .catch(function () {
        // Best-effort, like refresh(): keep showing the last good queue.
      });
  }

  // Lightweight refresh of just the Appointments tab's status strip (synced
  // time, pending count, last error) from /calendar/status/partial. The
  // iframe is deliberately left alone. See static/calendar.js, the caller.
  function refreshCalendarStatus() {
    return fetch("/calendar/status/partial", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var strip = document.getElementById("calendar-status");
        if (strip) strip.innerHTML = html;
      })
      .catch(function () {
        // Best-effort: keep showing the last good status.
      });
  }

  return { refresh: refresh, refreshQueue: refreshQueue, refreshCalendarStatus: refreshCalendarStatus };
})();
