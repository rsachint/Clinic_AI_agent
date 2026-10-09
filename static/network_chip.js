// The connection chip under the date in the sidebar: "Connection good" / "Connection slow" / "No connection",
// and a small popover with the three internet-dependent services (clinic/network_health.py decides; this only
// shows). Nothing else on the page changes when the connection is poor: no banner, no note on answers.
//
// Data: GET /network/status once on load and every 30 s, plus the `network_status` Socket.IO event the server
// sends when the state or a service's status changes. The page's one socket (window.clinicSocket, set by
// live_voice.js) is reused. The browser's own offline / online events show "No connection" at once (the server
// is told nothing) and refetch on coming back. Other scripts hear every update as the DOM event "networkstatus".
// Text is always put in with textContent, never as HTML.
//
// The pure helpers at the top are exported for tests/network_chip.test.js.
(function () {
  var VIEWS = {
    good: { label: "Connection good", cls: "net-good" },
    slow: { label: "Connection slow", cls: "net-slow" },
    down: { label: "No connection", cls: "net-down" },
  };
  var STATUS_CLASS = { ok: "net-ok", slow: "net-slow", down: "net-down" };

  // The chip's label and CSS class for a state ("good", "slow", "down"); anything else reads as good.
  function chipView(state) {
    return VIEWS[state] || VIEWS.good;
  }

  // The state to show: a browser that reports itself offline always means "down".
  function effectiveState(state, online) {
    return online === false ? "down" : state;
  }

  // The popover's title.
  function popoverTitle(state) {
    return state === "slow" || state === "down" ? "Connection is unstable" : "Connection is good";
  }

  // The popover's rows: [{ name, status, cls }] where status is e.g. "OK", "Slow", "Not responding", with the
  // failure ratio after it when there is one ("Slow · 2 of last 4 failed").
  function serviceLines(rows) {
    return (Array.isArray(rows) ? rows : []).map(function (row) {
      var status = String(row.label || "OK");
      if (row.detail) status += " · " + row.detail;
      return { name: String(row.name || ""), status: status, cls: STATUS_CLASS[row.status] || STATUS_CLASS.ok };
    });
  }

  // "Last checked 14:32 IST", or a plain note when nothing has been checked yet.
  function lastCheckedText(ist) {
    return ist ? "Last checked " + ist + " IST" : "Not checked yet";
  }

  // How long ago, for the tooltip: "just now", "5 min ago", "2 h ago" (epoch seconds; empty when unknown).
  function agoText(ts, nowMs) {
    if (typeof ts !== "number" || !isFinite(ts)) return "";
    var seconds = Math.max(0, Math.round((nowMs / 1000) - ts));
    if (seconds < 90) return "just now";
    var minutes = Math.round(seconds / 60);
    if (minutes < 90) return minutes + " min ago";
    return Math.round(minutes / 60) + " h ago";
  }

  var api = { chipView: chipView, effectiveState: effectiveState, popoverTitle: popoverTitle,
              serviceLines: serviceLines, lastCheckedText: lastCheckedText, agoText: agoText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  window.NetworkChip = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var chip = document.getElementById("net-chip");
    var label = document.getElementById("net-chip-label");
    var pop = document.getElementById("net-popover");
    var title = document.getElementById("net-pop-title");
    var list = document.getElementById("net-pop-list");
    var note = document.getElementById("net-pop-note");
    var checked = document.getElementById("net-pop-checked");
    var history = document.getElementById("net-pop-history");
    if (!chip || !label || !pop || !title || !list || !checked) return;

    var status = null;                       // the latest /network/status payload
    var online = !(typeof navigator !== "undefined" && navigator.onLine === false);

    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    function render() {
      var state = effectiveState(status ? status.state : "good", online);
      var view = chipView(state);
      chip.className = "net-chip " + view.cls;
      label.textContent = view.label;
      title.textContent = popoverTitle(state);
      clear(list);
      serviceLines(status ? status.services : []).forEach(function (line) {
        var item = document.createElement("li");
        var name = document.createElement("span");
        name.className = "net-pop-name";
        name.textContent = line.name;
        var value = document.createElement("span");
        value.className = "net-pop-status " + line.cls;
        value.textContent = line.status;
        item.appendChild(name);
        item.appendChild(value);
        list.appendChild(item);
      });
      if (note) {
        note.hidden = online;
        note.textContent = online ? "" : "This computer is not connected to the internet.";
      }
      checked.textContent = lastCheckedText(status ? status.last_checked : null);
      checked.title = agoText(status ? status.last_checked_ts : null, Date.now());
    }

    function apply(payload) {
      if (!payload || typeof payload.state !== "string") return;
      status = payload;
      render();
      document.dispatchEvent(new CustomEvent("networkstatus", { detail: payload }));
    }

    function load() {
      return fetch("/network/status").then(function (r) { return r.json(); })
        .then(function (r) { if (r && r.ok) apply(r); })
        .catch(function () { /* the chip keeps what it showed */ });
    }

    // -- the popover: a toggle button, closed by Escape or a click anywhere else --
    function isOpen() { return !pop.hidden; }
    function setOpen(open, refocus) {
      pop.hidden = !open;
      chip.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) render();
      if (!open && refocus) chip.focus();
    }
    chip.addEventListener("click", function () { setOpen(!isOpen()); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && isOpen()) setOpen(false, true);
    });
    document.addEventListener("click", function (event) {
      if (isOpen() && !pop.contains(event.target) && !chip.contains(event.target)) setOpen(false);
    });
    if (history) {
      history.addEventListener("click", function () {
        setOpen(false);
        if (window.ClinicNav) window.ClinicNav.select("audit");
        if (window.AuditSubtabs) window.AuditSubtabs.select("connection");
      });
    }

    // -- live updates --
    window.addEventListener("offline", function () { online = false; render(); });
    window.addEventListener("online", function () { online = true; render(); load(); });
    var socket = window.clinicSocket;
    if (socket && socket.on) {
      socket.on("network_status", apply);
      socket.on("connect", load);
    }
    setInterval(load, 30000);
    render();
    load();
  });
})();
