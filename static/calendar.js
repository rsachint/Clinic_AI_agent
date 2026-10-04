// Appointments tab: the Google Calendar embed, its Week / Month / Agenda
// buttons, "Sync now", and a light status-strip refresh.
//
// The iframe is only loaded the first time it is actually on screen (so the
// dashboard makes no request to calendar.google.com until someone opens the
// tab) and is NEVER reloaded by a refresh: that would reset the person's
// place in the calendar. Only the buttons change its src.
//
// The embed supports just Week / Month / Agenda. Voice "open the calendar"
// (static/live_voice.js) calls CalendarTab.show(mode).
document.addEventListener("DOMContentLoaded", function () {
  var section = document.querySelector('.tab-panel[data-tab="appointments"]');
  var frame = document.getElementById("cal-frame");
  if (!section) return;

  var STATUS_REFRESH_MS = 15000;
  var flash = document.getElementById("cal-flash");
  var syncButton = document.getElementById("cal-sync-btn");
  var preview = document.getElementById("cal-preview"); // present only when sync is not configured
  var mode = frame ? (frame.getAttribute("data-default-mode") || "week") : "week";
  var loadedMode = null;

  function showFlash(message, ok) {
    if (!flash) return;
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) setTimeout(function () { flash.hidden = true; }, 5000);
  }

  function highlightButtons() {
    section.querySelectorAll("[data-cal-mode]").forEach(function (button) {
      var active = button.getAttribute("data-cal-mode") === mode;
      button.classList.toggle("btn-queue-primary", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function isOnScreen() {
    if (!frame || section.hidden) return false;
    // A closed <details> ("View the calendar anyway") still has an
    // offsetParent in current browsers, so ask it directly.
    var wrapper = frame.closest("details");
    if (wrapper && !wrapper.open) return false;
    return frame.offsetParent !== null;
  }

  // Point the iframe at the current mode -- but only if it is visible, and
  // only if that is not already what it shows (so nothing ever reloads it
  // for no reason).
  function load() {
    if (!frame || !isOnScreen() || loadedMode === mode) return;
    var url = frame.getAttribute("data-src-" + mode);
    if (!url) return;
    frame.src = url;
    loadedMode = mode;
  }

  function setMode(next) {
    if (next !== "week" && next !== "month" && next !== "agenda") return;
    mode = next;
    highlightButtons();
    load();
  }

  section.addEventListener("click", function (event) {
    var modeButton = event.target.closest("[data-cal-mode]");
    if (modeButton) setMode(modeButton.getAttribute("data-cal-mode"));
  });

  if (preview) preview.addEventListener("toggle", load);

  function refreshStatus() {
    if (window.DashboardRefresh) return DashboardRefresh.refreshCalendarStatus();
    return Promise.resolve();
  }

  if (syncButton) {
    syncButton.addEventListener("click", function () {
      if (syncButton.disabled) return;
      syncButton.disabled = true;
      fetch("/calendar/sync", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (result) {
          if (result.ok) {
            showFlash("Sync queued. It runs in the background; the status above updates by itself.", true);
          } else {
            showFlash(result.error || "Could not queue a sync.", false);
          }
        })
        .catch(function (err) { showFlash("Request failed: " + err, false); })
        .then(function () {
          syncButton.disabled = false;
          refreshStatus();
          // The worker usually finishes within a few seconds.
          setTimeout(refreshStatus, 3000);
          setTimeout(refreshStatus, 8000);
        });
    });
  }

  document.addEventListener("tabchange", function (event) {
    if (event.detail && event.detail.id === "appointments") {
      highlightButtons();
      load();
      refreshStatus();
    }
  });

  setInterval(function () {
    if (!section.hidden && document.visibilityState === "visible") refreshStatus();
  }, STATUS_REFRESH_MS);

  highlightButtons();

  window.CalendarTab = {
    // Switch to the Appointments tab, in `requestedMode` if given. The mode
    // is set first so the iframe loads once, in the right view.
    show: function (requestedMode) {
      if (requestedMode === "week" || requestedMode === "month" || requestedMode === "agenda") {
        mode = requestedMode;
        highlightButtons();
      }
      if (window.ClinicNav) ClinicNav.select("appointments");
      load();
    },
    setMode: setMode,
  };
});
