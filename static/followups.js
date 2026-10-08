// Patients tab -> Follow-ups: the "Schedule follow-ups" batch card, the list of
// follow-ups with the state of each WhatsApp reminder, and the "Send manually"
// list for reminders WhatsApp would not let the app send.
//
// A follow-up is a return visit the doctor advised. Booking one puts a real slot in
// the calendar straight away and queues two reminders to the patient (the server
// does all of that: see clinic/followups.py). This file only draws it.
//
// Every value comes from the server as data and is inserted with textContent, never
// as HTML. The pure helpers at the top are exported for tests/ptt_client.test.js.

var FU_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
var FU_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "Wed 7 Oct" for an ISO date.
window.fuDayLabel = function (iso) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
  if (!m) return String(iso || "");
  var d = new Date(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10));
  return FU_DAYS[d.getDay()] + " " + d.getDate() + " " + FU_MONTHS[d.getMonth()];
};

window.fuVisitLabel = function (date, time) {
  return time ? window.fuDayLabel(date) + " " + time : window.fuDayLabel(date);
};

// The patient id for what was typed in the patient box: the datalist option whose
// text matches exactly. options = [{value, id}].
window.fuPatientId = function (text, options) {
  var wanted = String(text || "").trim();
  for (var i = 0; i < options.length; i++) {
    if (options[i].value === wanted) return parseInt(options[i].id, 10);
  }
  return null;
};

// What one row of the form sends to /followups/plan and /followups/apply.
window.fuRowPayload = function (fields) {
  var whole = function (v) { var n = parseInt(v, 10); return isNaN(n) ? null : n; };
  var note = String(fields.diagnosis || "").trim();
  return {
    patient_id: fields.patientId === undefined || fields.patientId === null ? null : whole(fields.patientId),
    due_date: fields.date || "", due_time: fields.time || "",
    doctor_id: whole(fields.doctor), branch_id: whole(fields.branch),
    diagnosis: note || null,
  };
};

// The badge for one reminder: {cls, text}.
window.fuReminderBadge = function (reminder) {
  var text = {
    queued: "Queued", sent: "Sent", blocked: "Blocked (outside 24h window)", failed: "Failed", skipped: "Skipped",
  }[reminder.status] || reminder.status;
  return { cls: "fu-badge fu-badge-" + reminder.status, text: text };
};

// "2 follow-ups booked, 1 could not be booked."
window.fuApplySummary = function (counts) {
  var line = counts.created + " follow-up" + (counts.created === 1 ? "" : "s") + " booked";
  if (counts.failed) line += ", " + counts.failed + " could not be booked";
  return line + ".";
};

// The same limits the server enforces on the reminder timing (clinic/settings.py),
// so a typo is caught before the request. Returns a list of messages (empty = fine).
window.fuTimingProblems = function (values) {
  var problems = [];
  var whole = function (v, label, low, high) {
    var text = String(v === undefined || v === null ? "" : v).trim();
    if (!/^\d+$/.test(text) || parseInt(text, 10) < low || parseInt(text, 10) > high) {
      problems.push(label + " must be a whole number from " + low + " to " + high + ".");
    }
  };
  var clock = function (v, label) {
    var m = /^([01]\d|2[0-3]):[0-5]\d$/.exec(String(v || "").trim());
    if (!m || parseInt(m[1], 10) < 5 || parseInt(m[1], 10) > 22) problems.push(label + " must be a time between 05:00 and 22:59.");
  };
  whole(values.days_before, "Days before", 1, 14);
  clock(values.send_time, "Send time");
  whole(values.hours_before, "Hours before", 1, 12);
  clock(values.earliest_send, "Earliest send time");
  return problems;
};

document.addEventListener("DOMContentLoaded", function () {
  var batchCard = document.getElementById("followup-batch-card");
  if (!batchCard) return;

  var rowsBox = document.getElementById("fu-rows");
  var reviewBox = document.getElementById("fu-review");
  var errorBox = document.getElementById("fu-error");
  var listBox = document.getElementById("fu-list");
  var manualBox = document.getElementById("fu-manual");
  var flash = document.getElementById("followups-flash");
  var panel = document.querySelector('.subtab-panel[data-subtab="followups"]');
  var todayIso = new Date().toISOString().slice(0, 10);

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function button(text, cls, onclick) {
    var b = el("button", cls || "btn-queue", text);
    b.type = "button";
    if (onclick) b.addEventListener("click", onclick);
    return b;
  }
  function labelled(text, control, cls) {
    var l = el("label", cls || null);
    l.appendChild(el("span", null, text));
    l.appendChild(control);
    return l;
  }
  function option(select, value, text) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    select.appendChild(o);
    return o;
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json(); });
  }
  function say(message, ok) {
    if (!flash) return;
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) setTimeout(function () { flash.hidden = true; }, 5000);
  }
  function fail(message) { errorBox.hidden = !message; errorBox.textContent = message || ""; }
  function multi() { return !!(window.Branches && Branches.multi()); }
  function doctors() { return window.Branches ? (Branches.data().doctors || []) : []; }

  function patientOptions() {
    return Array.prototype.map.call(document.querySelectorAll("#fu-patient-list option"), function (o) {
      return { value: o.value, id: o.getAttribute("data-id") };
    });
  }

  // ---- the batch form ---------------------------------------------------------
  var rows = [];

  function addRow(prefill) {
    prefill = prefill || {};
    var r = { node: el("div", "fu-row"), token: 0 };
    r.patient = el("input"); r.patient.type = "search"; r.patient.setAttribute("list", "fu-patient-list");
    r.patient.placeholder = "Search by name or phone"; r.patient.autocomplete = "off";
    r.date = el("input"); r.date.type = "date"; r.date.min = todayIso;
    r.doctor = el("select"); option(r.doctor, "", "Doctor on duty");
    doctors().forEach(function (d) { option(r.doctor, d.id, d.name); });
    r.branch = el("select");
    r.time = el("select"); option(r.time, "", "Pick a date first");
    r.diagnosis = el("input"); r.diagnosis.type = "text"; r.diagnosis.maxLength = 500;
    r.diagnosis.placeholder = "Optional. Internal only, never sent to the patient";
    r.note = el("div", "fu-row-note"); r.note.hidden = true;

    var branchLabel = labelled("Branch", r.branch, "branch-field");
    var grid = el("div", "appt-form-grid");
    grid.appendChild(labelled("Patient", r.patient, "fu-patient-label"));
    grid.appendChild(labelled("Follow-up date", r.date));
    grid.appendChild(labelled("Doctor", r.doctor));
    grid.appendChild(branchLabel);
    grid.appendChild(labelled("Time", r.time));
    grid.appendChild(labelled("Diagnosis (internal)", r.diagnosis, "fu-diagnosis-label"));
    r.node.appendChild(grid);
    r.node.appendChild(r.note);
    var remove = button("Remove row", "btn-queue btn-queue-danger fu-remove", function () {
      rows.splice(rows.indexOf(r), 1);
      r.node.remove();
      if (!rows.length) addRow();
    });
    r.node.appendChild(remove);
    rowsBox.appendChild(r.node);
    rows.push(r);

    fillBranches(r, prefill.branchId);
    r.date.addEventListener("change", function () { loadSlots(r); });
    r.doctor.addEventListener("change", function () { loadSlots(r); });
    r.branch.addEventListener("change", function () { loadSlots(r); });
    if (prefill.date) { r.date.value = prefill.date; loadSlots(r, prefill.time); }
    return r;
  }

  function fillBranches(r, chosen) {
    var label = r.branch.closest(".branch-field");
    if (label) label.hidden = !multi();
    var keep = chosen || r.branch.value;
    r.branch.innerHTML = "";
    if (window.Branches) {
      Branches.list().forEach(function (b) { option(r.branch, b.id, b.name + (b.status === "closed" ? " (closed)" : "")); });
      r.branch.value = String(keep || Branches.bookingBranch());
    }
  }

  function showNote(r, message, suggestion) {
    r.note.innerHTML = "";
    r.note.hidden = !message;
    if (!message) return;
    r.note.appendChild(el("span", null, message + " "));
    if (suggestion) {
      r.note.appendChild(button("Use " + suggestion.label + (suggestion.doctor ? " (" + suggestion.doctor + ")" : ""), "btn-queue", function () {
        r.date.value = suggestion.date;
        loadSlots(r, suggestion.time);
      }));
    }
  }

  function loadSlots(r, wantedTime) {
    var keep = wantedTime || r.time.value;
    var mine = ++r.token;
    r.time.innerHTML = "";
    showNote(r, "");
    if (!r.date.value) { option(r.time, "", "Pick a date first"); return; }
    option(r.time, "", "Loading...");
    var url = "/followups/slots?date=" + encodeURIComponent(r.date.value) +
      (r.branch.value ? "&branch=" + encodeURIComponent(r.branch.value) : "") +
      (r.doctor.value ? "&doctor=" + encodeURIComponent(r.doctor.value) : "");
    fetch(url).then(function (res) { return res.json(); }).then(function (data) {
      if (mine !== r.token) return;
      r.time.innerHTML = "";
      if (!data.ok) { option(r.time, "", data.error || "Could not load times"); return; }
      option(r.time, "", data.free.length ? "Pick a time" : "No free times");
      data.free.forEach(function (t) { option(r.time, t, t); });
      if (keep && data.free.indexOf(keep) >= 0) r.time.value = keep;
      if (data.problem) showNote(r, data.problem + " Nobody can be booked that day.", data.suggestion);
      else if (!data.free.length) showNote(r, "No free times that day.", data.suggestion);
      else r.note.hidden = true;
    }).catch(function () {
      if (mine !== r.token) return;
      r.time.innerHTML = "";
      option(r.time, "", "Could not load times");
    });
  }

  function payload(r) {
    return window.fuRowPayload({
      patientId: window.fuPatientId(r.patient.value, patientOptions()),
      date: r.date.value, time: r.time.value, doctor: r.doctor.value, branch: r.branch.value, diagnosis: r.diagnosis.value,
    });
  }
  function payloads() { return rows.map(payload); }

  // ---- review -------------------------------------------------------------------
  function review() {
    fail("");
    reviewBox.innerHTML = "";
    post("/followups/plan", { rows: payloads() }).then(function (r) {
      if (!r.ok) { fail(r.error || "Could not check the follow-ups."); return; }
      reviewBox.appendChild(renderReview(r.plan));
    }).catch(function (err) { fail("Could not check the follow-ups: " + err.message); });
  }

  function renderReview(plan) {
    var card = el("div", "closure-card fu-review-card");
    var c = plan.counts;
    card.appendChild(el("h3", "closure-title", "Review"));
    card.appendChild(el("div", "muted", c.valid + " of " + c.total + " ready to book" + (c.invalid ? ", " + c.invalid + " need a change" : "") + "."));
    var table = el("table", "closure-table");
    var head = el("tr");
    ["Patient", "Visit", "With", "Check"].forEach(function (h) { head.appendChild(el("th", null, h)); });
    table.appendChild(head);
    plan.rows.forEach(function (row, i) {
      var tr = el("tr", row.ok ? null : "fu-bad");
      tr.appendChild(el("td", null, row.patient || "(choose a patient)"));
      tr.appendChild(el("td", null, row.row.due_date ? window.fuVisitLabel(row.row.due_date, row.row.due_time) : "-"));
      var who = row.doctor || "-";
      if (multi() && row.branch) who += " · " + row.branch;
      tr.appendChild(el("td", null, who));
      var check = el("td", "fu-check");
      if (row.ok) check.appendChild(el("span", "fu-badge fu-badge-sent", "Ready"));
      row.errors.forEach(function (e) { check.appendChild(el("div", "fu-problem", e)); });
      row.warnings.forEach(function (w) { check.appendChild(el("div", "muted fu-warning", w)); });
      if (row.suggestion) {
        var s = row.suggestion;
        check.appendChild(button("Use " + s.label, "btn-queue", function () {
          if (rows[i]) { rows[i].date.value = s.date; loadSlots(rows[i], s.time); }
          review();
        }));
      }
      tr.appendChild(check);
      table.appendChild(tr);
    });
    var wrap = el("div", "closure-table-wrap");
    wrap.appendChild(table);
    card.appendChild(wrap);

    var status = el("div", "closure-status"); status.hidden = true;
    var actions = el("div", "closure-actions");
    var apply = button(c.valid ? "Book " + c.valid + " follow-up" + (c.valid === 1 ? "" : "s") : "Nothing to book", "btn-confirm");
    apply.disabled = !c.valid;
    var back = button("Back to editing", "btn-reject", function () { reviewBox.innerHTML = ""; });
    actions.appendChild(apply);
    actions.appendChild(back);
    card.appendChild(status);
    card.appendChild(actions);
    apply.addEventListener("click", function () { applyBatch(card, status, actions, apply, back); });
    return card;
  }

  function applyBatch(card, status, actions, apply, back) {
    apply.disabled = true; back.disabled = true;
    function say2(text, ok) { status.hidden = false; status.className = "closure-status " + (ok ? "closure-ok" : "closure-bad"); status.textContent = text; }
    post("/followups/apply", { rows: payloads() }).then(function (result) {
      if (!result.ok) { say2(result.error || "Could not book the follow-ups.", false); apply.disabled = false; back.disabled = false; return; }
      say2(window.fuApplySummary(result.counts), !result.counts.failed);
      result.results.forEach(function (r) {
        status.appendChild(el("div", r.ok ? "fu-done" : "closure-fail",
          (r.patient || "Row " + (r.index + 1)) + " (" + window.fuVisitLabel(r.due_date, r.due_time) + "): " +
          (r.ok ? "booked" + (r.doctor ? " with " + r.doctor : "") : r.error)));
      });
      // Rows that were booked leave the form; the ones that failed stay so they can be fixed.
      var done = {};
      result.results.forEach(function (r) { if (r.ok) done[r.index] = true; });
      var keep = [];
      rows.forEach(function (row, i) { if (done[i]) row.node.remove(); else keep.push(row); });
      rows = keep;
      if (!rows.length) addRow();
      actions.innerHTML = "";
      if (result.batch_id) {
        var undo = button("Undo this batch", "btn-queue", function () {
          if (!window.confirm("Cancel these follow-ups and free their calendar slots?")) return;
          undo.disabled = true;
          post("/followups/batches/" + result.batch_id + "/undo").then(function (u) {
            say2(u.message || u.error || "Done.", !!u.ok);
            if (u.ok) undo.remove(); else undo.disabled = false;
            load();
          });
        });
        actions.appendChild(undo);
      }
      actions.appendChild(button("Close", "btn-queue", function () { reviewBox.innerHTML = ""; }));
      load();
      if (window.DashboardRefresh) window.DashboardRefresh.refreshQueue();
    }).catch(function (err) { say2("Could not book the follow-ups: " + err.message, false); apply.disabled = false; back.disabled = false; });
  }

  document.getElementById("fu-add-row").addEventListener("click", function () { addRow(); });
  document.getElementById("fu-review-btn").addEventListener("click", review);

  // ---- the list -----------------------------------------------------------------
  function followupBadge(item, today) {
    if (!item.has_slot) return el("span", "fu-badge fu-badge-skipped", "Date only");
    if (item.appointment_status === "no_show") return el("span", "fu-badge fu-badge-failed", "No-show");
    var label = { pending: item.due_date < today ? "Overdue" : "Upcoming", done: "Done", cancelled: "Cancelled", missed: "Missed" }[item.status] || item.status;
    var cls = { pending: item.due_date < today ? "failed" : "queued", done: "sent", cancelled: "skipped", missed: "failed" }[item.status] || "skipped";
    return el("span", "fu-badge fu-badge-" + cls, label);
  }

  function diagnosisCell(item) {
    var cell = el("td", "fu-diagnosis-cell");
    var input = el("input"); input.type = "text"; input.maxLength = 500; input.value = item.diagnosis || "";
    input.placeholder = "Internal note"; input.setAttribute("aria-label", "Internal diagnosis note for " + item.patient_name);
    var note = el("span", "muted fu-saved");
    var save = button("Save", "btn-queue", function () {
      save.disabled = true;
      post("/followups/" + item.id + "/diagnosis", { diagnosis: input.value }).then(function (r) {
        if (!r.ok) { note.textContent = r.error || "Could not save."; save.disabled = false; return; }
        item.diagnosis = r.diagnosis; input.value = r.diagnosis; save.hidden = true; save.disabled = false;
        note.textContent = "";
        SaveTick.show(save);
      }).catch(function () { note.textContent = "Could not save."; save.disabled = false; });
    });
    save.hidden = true;
    input.addEventListener("input", function () { save.hidden = input.value.trim() === (item.diagnosis || ""); note.textContent = ""; SaveTick.clear(cell); });
    input.addEventListener("keydown", function (event) { if (event.key === "Enter" && !save.hidden) { event.preventDefault(); save.click(); } });
    cell.appendChild(input);
    cell.appendChild(save);
    cell.appendChild(note);
    return cell;
  }

  function remindersCell(item) {
    var cell = el("td", "fu-reminders");
    if (!item.has_slot) { cell.appendChild(el("span", "muted", "No slot, no reminders")); return cell; }
    if (!item.reminders.length) { cell.appendChild(el("span", "muted", "-")); return cell; }
    item.reminders.forEach(function (rem) {
      var line = el("div", "fu-rem");
      line.appendChild(el("span", "fu-rem-label", rem.label + ": "));
      var badge = window.fuReminderBadge(rem);
      line.appendChild(el("span", badge.cls, badge.text));
      if (rem.detail) line.appendChild(el("div", "muted fu-rem-detail", rem.detail));
      if (rem.retryable) {
        var retry = button("Retry", "btn-queue", function () {
          retry.disabled = true;
          post("/followups/reminders/" + rem.id + "/retry").then(function (r) {
            if (!r.ok) say(r.error || "Could not retry.", false);
            load();
          });
        });
        line.appendChild(retry);
      }
      cell.appendChild(line);
    });
    return cell;
  }

  function renderList(items, today) {
    listBox.innerHTML = "";
    document.getElementById("fu-list-count").textContent = items.length ? "(" + items.length + ")" : "";
    if (!items.length) { listBox.appendChild(el("p", "muted", "No follow-ups for this branch yet.")); return; }
    var table = el("table", "fu-table");
    var head = el("tr");
    ["Patient", "Visit", "Doctor / branch", "Status", "Reminders", "Diagnosis (internal)"].forEach(function (h) { head.appendChild(el("th", null, h)); });
    table.appendChild(head);
    items.forEach(function (item) {
      var tr = el("tr", item.status === "pending" ? null : "fu-past");
      var who = el("td");
      who.appendChild(el("strong", null, item.patient_name));
      who.appendChild(el("div", "muted", item.phone || ""));
      if (item.opted_out) who.appendChild(el("div", "fu-problem", "Asked to stop reminders"));
      tr.appendChild(who);
      tr.appendChild(el("td", "fu-visit", item.has_slot ? window.fuVisitLabel(item.due_date, item.due_time) : window.fuDayLabel(item.due_date)));
      var where = item.doctor || "-";
      if (multi()) where += " · " + item.branch;
      tr.appendChild(el("td", null, item.has_slot ? where : "-"));
      var st = el("td"); st.appendChild(followupBadge(item, today)); tr.appendChild(st);
      tr.appendChild(remindersCell(item));
      tr.appendChild(diagnosisCell(item));
      table.appendChild(tr);
    });
    var wrap = el("div", "closure-table-wrap"); wrap.appendChild(table); listBox.appendChild(wrap);
  }

  // ---- send manually --------------------------------------------------------------
  // The clipboard API needs a secure page and a focused window; when it says no, the
  // old select-and-copy trick still works on plain http://localhost.
  function copyViaSelection(text) {
    return new Promise(function (resolve, reject) {
      var area = el("textarea"); area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
      document.body.appendChild(area); area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      area.remove();
      ok ? resolve() : reject(new Error("copy blocked"));
    });
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { return copyViaSelection(text); });
    }
    return copyViaSelection(text);
  }

  function renderManual(items) {
    manualBox.innerHTML = "";
    document.getElementById("fu-manual-count").textContent = items.length ? "(" + items.length + ")" : "";
    if (!items.length) { manualBox.appendChild(el("p", "muted", "Nothing to send by hand.")); return; }
    items.forEach(function (m) {
      var card = el("div", "fu-manual-item");
      var head = el("div", "fu-manual-head");
      head.appendChild(el("strong", null, m.patient_name));
      head.appendChild(el("span", "muted", " · " + (m.phone || "no phone") + " · visit " + m.visit + " · " + m.label));
      head.appendChild(el("span", "fu-badge fu-badge-" + m.status, m.status === "blocked" ? "Blocked (outside 24h window)" : "Failed"));
      card.appendChild(head);
      if (m.error) card.appendChild(el("div", "muted fu-rem-detail", m.error));
      card.appendChild(el("div", "fu-message-text", m.text));
      var actions = el("div", "closure-actions");
      var copy = button("Copy text", "btn-queue", function () {
        copyText(m.text).then(function () { copy.textContent = "Copied"; setTimeout(function () { copy.textContent = "Copy text"; }, 2000); })
          .catch(function () { copy.textContent = "Copy failed: select the text"; });
      });
      var sent = button("Mark as sent manually", "btn-queue btn-queue-ok", function () {
        if (!window.confirm("Mark this reminder as sent by hand? It will not be sent or retried by the app.")) return;
        sent.disabled = true;
        post("/followups/reminders/" + m.reminder_id + "/manual-sent").then(function (r) {
          if (!r.ok) say(r.error || "Could not record that.", false);
          load();
        });
      });
      var retry = button("Retry sending", "btn-queue", function () {
        retry.disabled = true;
        post("/followups/reminders/" + m.reminder_id + "/retry").then(function (r) { if (!r.ok) say(r.error || "Could not retry.", false); load(); });
      });
      actions.appendChild(copy); actions.appendChild(sent); actions.appendChild(retry);
      card.appendChild(actions);
      manualBox.appendChild(card);
    });
  }

  // ---- loading --------------------------------------------------------------------
  var request = 0;
  function load() {
    var mine = ++request;
    var query = window.Branches ? "?" + Branches.viewQuery() : "";
    return fetch("/followups/data" + query).then(function (r) { return r.json(); }).then(function (data) {
      if (mine !== request || !data.ok) return;
      renderList(data.followups, data.today);
      renderManual(data.manual);
    }).catch(function () {
      if (mine === request) { listBox.innerHTML = ""; listBox.appendChild(el("p", "muted", "Could not load the follow-ups.")); }
    });
  }

  function visible() { return !!panel && !panel.hidden && !panel.closest(".tab-panel").hidden; }
  document.addEventListener("subtabchange", function (event) { if (event.detail && event.detail.id === "followups") load(); });
  document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "patients" && visible()) load(); });
  document.addEventListener("branchchange", function () { rows.forEach(function (r) { fillBranches(r); }); load(); });
  document.addEventListener("branchdata", function () { rows.forEach(function (r) { fillBranches(r); }); load(); });
  setInterval(function () { if (visible() && document.visibilityState === "visible") load(); }, 30000);

  addRow();
  load();
});

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    fuDayLabel: window.fuDayLabel, fuVisitLabel: window.fuVisitLabel, fuPatientId: window.fuPatientId,
    fuRowPayload: window.fuRowPayload, fuReminderBadge: window.fuReminderBadge, fuApplySummary: window.fuApplySummary,
    fuTimingProblems: window.fuTimingProblems,
  };
}
