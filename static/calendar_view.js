// Appointments tab: the clinic's own calendar, drawn from the database (no
// Google). Week / Month / Agenda views, previous / next / Today, and a branch
// filter that follows the sidebar switcher (default: My branch). Hovering (or
// tabbing to) an appointment shows its details in a floating card; clicking it
// opens it in the Queue tab, where it can be moved or cancelled. Voice "open the calendar"
// (static/live_voice.js) calls CalendarTab.show(mode).
//
// The date helpers are pure (UTC arithmetic on ISO dates, so the browser's
// time zone never shifts a day) and are exported for tests/ptt_client.test.js.

var CV_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
var CV_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function cvParse(iso) {
  var p = String(iso).split("-");
  return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
}
function cvIso(d) {
  return d.getUTCFullYear() + "-" + ("0" + (d.getUTCMonth() + 1)).slice(-2) + "-" + ("0" + d.getUTCDate()).slice(-2);
}
window.cvAddDays = function (iso, n) {
  var d = cvParse(iso);
  d.setUTCDate(d.getUTCDate() + n);
  return cvIso(d);
};
window.cvWeekStart = function (iso) {              // the Monday on or before `iso`
  var d = cvParse(iso);
  return window.cvAddDays(iso, -((d.getUTCDay() + 6) % 7));
};
window.cvMonthRange = function (iso) {             // whole weeks covering the month of `iso`
  var d = cvParse(iso);
  var first = cvIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1)));
  var last = cvIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)));
  return { start: window.cvWeekStart(first), end: window.cvAddDays(window.cvWeekStart(last), 6) };
};
window.cvRange = function (mode, anchor) {
  if (mode === "month") return window.cvMonthRange(anchor);
  if (mode === "agenda") return { start: anchor, end: window.cvAddDays(anchor, 13) };
  var start = window.cvWeekStart(anchor);
  return { start: start, end: window.cvAddDays(start, 6) };
};
window.cvShift = function (mode, anchor, direction) {
  if (mode === "month") {
    var d = cvParse(anchor);
    return cvIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + direction, 1)));
  }
  return window.cvAddDays(anchor, direction * (mode === "agenda" ? 14 : 7));
};
window.cvTitle = function (mode, anchor) {
  var r = window.cvRange(mode, anchor);
  var a = cvParse(r.start), b = cvParse(r.end);
  if (mode === "month") { var m = cvParse(anchor); return CV_MONTHS[m.getUTCMonth()] + " " + m.getUTCFullYear(); }
  return a.getUTCDate() + " " + CV_MONTHS[a.getUTCMonth()].slice(0, 3) + " - " + b.getUTCDate() + " " +
    CV_MONTHS[b.getUTCMonth()].slice(0, 3) + " " + b.getUTCFullYear();
};
window.cvDayLabel = function (iso) {
  var d = cvParse(iso);
  return CV_DAYS[(d.getUTCDay() + 6) % 7] + " " + d.getUTCDate() + " " + CV_MONTHS[d.getUTCMonth()].slice(0, 3);
};
window.cvMinutes = function (hhmm) {
  var p = String(hhmm).split(":");
  return +p[0] * 60 + +p[1];
};
// The visible hours of the week grid: 08:00-20:00 at least, wider if something falls outside.
window.cvHourBounds = function (appointments) {
  var lo = 8 * 60, hi = 20 * 60;
  appointments.forEach(function (a) {
    lo = Math.min(lo, Math.floor(window.cvMinutes(a.start_time) / 60) * 60);
    hi = Math.max(hi, Math.ceil(window.cvMinutes(a.end_time) / 60) * 60);
  });
  return { start: lo, end: hi };
};
window.cvGroupByDay = function (appointments) {
  var days = {};
  appointments.forEach(function (a) { (days[a.appt_date] = days[a.appt_date] || []).push(a); });
  return days;
};

document.addEventListener("DOMContentLoaded", function () {
  var section = document.querySelector('.tab-panel[data-tab="appointments"]');
  var panel = document.getElementById("calendar-panel");
  var body = document.getElementById("cv-body");
  if (!section || !panel || !body) return;

  var HOUR_PX = 52;
  var today = panel.getAttribute("data-today") || cvIso(new Date());
  var mode = "week";
  var anchor = today;
  var data = { appointments: [], blocks: [] };
  var requestId = 0;

  var title = document.getElementById("cv-title");
  var branchSelect = document.getElementById("cv-branch");
  var legend = document.getElementById("cv-legend");
  var tip = null;   // the floating details card, made on first hover

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function branchColor(a) { return a.branch_color || "#1a6dd6"; }
  function label(a) { return (a.token ? a.token + " · " : "") + (a.patient_name || "(no name)"); }
  function inClosure(iso, blockList) {
    return blockList.filter(function (b) { return b.start_date <= iso && iso <= b.end_date; });
  }

  // ---- drawing ------------------------------------------------------------
  function draw() {
    title.textContent = window.cvTitle(mode, anchor);
    section.querySelectorAll("[data-cal-mode]").forEach(function (button) {
      var active = button.getAttribute("data-cal-mode") === mode;
      button.classList.toggle("btn-queue-primary", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    hideTip();
    body.innerHTML = "";
    if (mode === "month") drawMonth();
    else if (mode === "agenda") drawAgenda();
    else drawWeek();
    drawLegend();
  }

  function drawLegend() {
    legend.innerHTML = "";
    if (!window.Branches || !Branches.multi()) return;
    Branches.list().forEach(function (b) {
      var chip = h("span", "cv-legend-chip");
      var dot = h("span", "cv-dot");
      dot.style.background = b.color || "#1a6dd6";
      chip.appendChild(dot);
      chip.appendChild(document.createTextNode(b.name));
      legend.appendChild(chip);
    });
  }

  function block(a, style) {
    var el = h("button", "cv-appt");
    el.type = "button";
    el.style.borderLeftColor = branchColor(a);
    Object.keys(style || {}).forEach(function (k) { el.style[k] = style[k]; });
    el.appendChild(h("span", "cv-appt-time", a.start_time));
    el.appendChild(h("span", "cv-appt-name", label(a)));
    hoverDetails(el, a);
    el.addEventListener("click", function (event) { event.stopPropagation(); openInQueue(a); });
    return el;
  }

  function drawWeek() {
    var range = window.cvRange("week", anchor);
    var byDay = window.cvGroupByDay(data.appointments);
    var bounds = window.cvHourBounds(data.appointments);
    var grid = h("div", "cv-week");

    grid.appendChild(h("div", "cv-corner"));
    for (var i = 0; i < 7; i++) {
      var iso = window.cvAddDays(range.start, i);
      var head = h("div", "cv-dayhead" + (iso === today ? " cv-today" : ""), window.cvDayLabel(iso));
      grid.appendChild(head);
    }
    var gutter = h("div", "cv-gutter");
    gutter.style.height = ((bounds.end - bounds.start) / 60 * HOUR_PX) + "px";
    for (var m = bounds.start; m < bounds.end; m += 60) {
      var tick = h("div", "cv-hour", ("0" + (m / 60)).slice(-2) + ":00");
      tick.style.top = ((m - bounds.start) / 60 * HOUR_PX) + "px";
      gutter.appendChild(tick);
    }
    grid.appendChild(gutter);
    for (var d = 0; d < 7; d++) {
      var dayIso = window.cvAddDays(range.start, d);
      var col = h("div", "cv-daycol" + (dayIso === today ? " cv-today" : ""));
      col.style.height = ((bounds.end - bounds.start) / 60 * HOUR_PX) + "px";
      inClosure(dayIso, data.blocks).forEach(function (b) {
        var band = h("div", "cv-closure", (b.branch ? b.branch + ": " : "") + (b.reason || "Closed"));
        if (b.start_time) {
          band.style.top = ((window.cvMinutes(b.start_time) - bounds.start) / 60 * HOUR_PX) + "px";
          band.style.height = ((window.cvMinutes(b.end_time) - window.cvMinutes(b.start_time)) / 60 * HOUR_PX) + "px";
        }
        col.appendChild(band);
      });
      (byDay[dayIso] || []).forEach(function (a) {
        var top = (window.cvMinutes(a.start_time) - bounds.start) / 60 * HOUR_PX;
        var height = Math.max(22, (window.cvMinutes(a.end_time) - window.cvMinutes(a.start_time)) / 60 * HOUR_PX - 2);
        col.appendChild(block(a, { top: top + "px", height: height + "px" }));
      });
      grid.appendChild(col);
    }
    body.appendChild(grid);
  }

  function drawMonth() {
    var range = window.cvRange("month", anchor);
    var byDay = window.cvGroupByDay(data.appointments);
    var month = cvParse(anchor).getUTCMonth();
    var grid = h("div", "cv-month");
    CV_DAYS.forEach(function (name) { grid.appendChild(h("div", "cv-dayhead", name)); });
    for (var iso = range.start; iso <= range.end; iso = window.cvAddDays(iso, 1)) {
      var cell = h("div", "cv-cell" + (iso === today ? " cv-today" : "") + (cvParse(iso).getUTCMonth() !== month ? " cv-other" : ""));
      cell.appendChild(h("div", "cv-celldate", String(cvParse(iso).getUTCDate())));
      if (inClosure(iso, data.blocks).length) cell.classList.add("cv-closed");
      var items = byDay[iso] || [];
      items.slice(0, 3).forEach(function (a) { cell.appendChild(block(a)); });
      if (items.length > 3) cell.appendChild(h("div", "cv-more", "+" + (items.length - 3) + " more"));
      (function (day) {
        cell.addEventListener("click", function () { anchor = day; mode = "week"; load(); });
      })(iso);
      grid.appendChild(cell);
    }
    body.appendChild(grid);
  }

  function drawAgenda() {
    var range = window.cvRange("agenda", anchor);
    var byDay = window.cvGroupByDay(data.appointments);
    var list = h("div", "cv-agenda");
    var any = false;
    for (var iso = range.start; iso <= range.end; iso = window.cvAddDays(iso, 1)) {
      var items = byDay[iso] || [];
      var closures = inClosure(iso, data.blocks);
      if (!items.length && !closures.length) continue;
      any = true;
      var day = h("div", "cv-agenda-day");
      day.appendChild(h("div", "cv-agenda-date" + (iso === today ? " cv-today" : ""), window.cvDayLabel(iso)));
      closures.forEach(function (b) { day.appendChild(h("div", "cv-closure cv-closure-row", (b.branch ? b.branch + ": " : "") + (b.reason || "Closed"))); });
      items.forEach(function (a) {
        var row = h("button", "cv-agenda-row");
        row.type = "button";
        row.style.borderLeftColor = branchColor(a);
        row.appendChild(h("span", "cv-agenda-time", a.start_time + "-" + a.end_time));
        row.appendChild(h("span", "cv-agenda-name", label(a)));
        row.appendChild(h("span", "muted", [a.branch, a.doctor].filter(Boolean).join(" · ")));
        if (a.status !== "booked" && a.status !== "confirmed") row.appendChild(h("span", "cv-status", a.status.replace("_", " ")));
        hoverDetails(row, a);
        row.addEventListener("click", function () { openInQueue(a); });
        day.appendChild(row);
      });
      list.appendChild(day);
    }
    if (!any) list.appendChild(h("p", "muted", "No appointments in these two weeks."));
    body.appendChild(list);
  }

  // ---- hover details --------------------------------------------------------
  function openInQueue(a) {
    hideTip();
    var picker = document.getElementById("queue-date");
    if (picker) { picker.value = a.appt_date; picker.dispatchEvent(new Event("change")); }
    if (window.Branches) Branches.setView(a.branch_id);
    if (window.ClinicNav) ClinicNav.select("queue");
  }

  function hideTip() { if (tip) tip.hidden = true; }

  function fillTip(a) {
    tip.innerHTML = "";
    // The lines are static/appt_details.js, shared with the Cancel / Reschedule review card.
    window.ApptDetails.lines(a).forEach(function (line) {
      tip.appendChild(line.kind === "name" ? h("strong", null, line.text)
        : h("div", line.kind === "place" ? null : "muted", line.text));
    });
    tip.appendChild(h("div", "cv-tip-hint", "Click to open in Queue"));
  }

  // Beside the event, flipped or nudged so it never runs off the screen.
  function placeTip(target) {
    var r = target.getBoundingClientRect();
    var w = tip.offsetWidth, hgt = tip.offsetHeight, gap = 8;
    var left = r.right + gap;
    if (left + w > window.innerWidth - 8) left = Math.max(8, r.left - w - gap);
    if (left < 8) left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    var top = Math.max(8, Math.min(r.top, window.innerHeight - hgt - 8));
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function hoverDetails(el, a) {
    function show() {
      if (!tip) {
        tip = h("div", "cv-tip");
        tip.setAttribute("role", "tooltip");
        document.body.appendChild(tip);
      }
      fillTip(a);
      tip.hidden = false;
      placeTip(el);
    }
    el.addEventListener("mouseenter", show);
    el.addEventListener("focus", show);
    el.addEventListener("mouseleave", hideTip);
    el.addEventListener("blur", hideTip);
  }

  // ---- data ---------------------------------------------------------------
  var retriesLeft = 0;
  var loadedOnce = false;
  function load(isRetry) {
    if (isRetry !== true) retriesLeft = 3;
    var range = window.cvRange(mode, anchor);
    var mine = ++requestId;
    var branch = window.Branches ? Branches.view() : "";
    title.textContent = window.cvTitle(mode, anchor);
    return fetch("/calendar/data?start=" + range.start + "&end=" + range.end + "&branch=" + encodeURIComponent(branch))
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (mine !== requestId) return;
        if (!result.ok) throw new Error(result.error || "Could not load the calendar.");
        data = result;
        loadedOnce = true;
        draw();
      })
      .catch(function (err) {
        if (mine !== requestId) return;
        // A dropped connection (the server restarting, wifi blinking) is usually
        // over in seconds: try again soon, and keep what is already drawn.
        var network = err instanceof TypeError;
        if (network && retriesLeft > 0) {
          retriesLeft -= 1;
          setTimeout(function () { if (mine === requestId) load(true); }, 3000);
          if (loadedOnce) return;
        }
        body.innerHTML = "";
        body.appendChild(h("div", "flash error", network ? "Can't reach the server. Retrying\u2026" : String(err.message || err)));
      });
  }

  function fillBranchSelect() {
    if (!branchSelect || !window.Branches) return;
    var wrap = branchSelect.closest(".cv-branch");
    if (wrap) wrap.hidden = !Branches.multi();
    branchSelect.innerHTML = "";
    var all = h("option", null, "All branches");
    all.value = "all";
    branchSelect.appendChild(all);
    Branches.list().forEach(function (b) {
      var o = h("option", null, b.name + (b.id === Branches.mine() ? " ★" : ""));
      o.value = b.id;
      branchSelect.appendChild(o);
    });
    branchSelect.value = String(Branches.view());
  }

  // ---- wiring -------------------------------------------------------------
  section.querySelectorAll("[data-cal-mode]").forEach(function (button) {
    button.addEventListener("click", function () { mode = button.getAttribute("data-cal-mode"); load(); });
  });
  document.getElementById("cv-prev").addEventListener("click", function () { anchor = window.cvShift(mode, anchor, -1); load(); });
  document.getElementById("cv-next").addEventListener("click", function () { anchor = window.cvShift(mode, anchor, 1); load(); });
  document.getElementById("cv-today").addEventListener("click", function () { anchor = today; load(); });
  if (branchSelect) branchSelect.addEventListener("change", function () { Branches.setView(branchSelect.value); });
  document.addEventListener("branchchange", function () { fillBranchSelect(); if (!section.hidden) load(); });
  document.addEventListener("branchdata", function () { fillBranchSelect(); if (!section.hidden) load(); });
  document.addEventListener("tabchange", function (event) {
    if (event.detail && event.detail.id === "appointments") { fillBranchSelect(); load(); }
  });
  setInterval(function () { if (!section.hidden && document.visibilityState === "visible") load(); }, 60000);

  // Voice: "open the calendar" / "show the month view".
  window.CalendarTab = {
    show: function (requested, day) {
      if (requested === "week" || requested === "month" || requested === "agenda") mode = requested;
      if (day) anchor = day;
      if (window.ClinicNav) ClinicNav.select("appointments");
      else load();
    },
  };

  fillBranchSelect();
});

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cvAddDays: window.cvAddDays, cvWeekStart: window.cvWeekStart, cvMonthRange: window.cvMonthRange, cvRange: window.cvRange,
    cvShift: window.cvShift, cvTitle: window.cvTitle, cvDayLabel: window.cvDayLabel, cvHourBounds: window.cvHourBounds,
    cvGroupByDay: window.cvGroupByDay,
  };
}
