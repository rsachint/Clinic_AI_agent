// Queue tab behaviour: row action buttons, notification Retry buttons, and
// the ~15s auto-refresh while the tab is on screen.
//
// The panel's HTML is re-rendered by the server (see templates/
// _queue_panel.html and DashboardRefresh.refreshQueue), so clicks are
// handled by one delegated listener on #queue-panel rather than per-button
// listeners that would be lost on every refresh.
document.addEventListener("DOMContentLoaded", function () {
  var panel = document.getElementById("queue-panel");
  var flash = document.getElementById("queue-flash");
  var section = document.querySelector('.tab-panel[data-tab="queue"]');
  if (!panel || !section) return;

  var REFRESH_MS = 15000;

  function showFlash(message, ok) {
    if (!flash) return;
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) {
      setTimeout(function () { flash.hidden = true; }, 4000);
    }
  }

  function refresh() {
    if (window.DashboardRefresh) return DashboardRefresh.refreshQueue();
    return Promise.resolve();
  }

  function post(url) {
    return fetch(url, { method: "POST" }).then(function (r) { return r.json(); });
  }

  panel.addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button || button.disabled) return;

    var action = button.getAttribute("data-queue-action");
    var retryId = button.getAttribute("data-notification-retry");
    if (!action && !retryId) return;

    button.disabled = true;
    var request = action
      ? post("/queue/" + button.getAttribute("data-appointment-id") + "/" + action)
      : post("/notifications/" + retryId + "/retry");

    request
      .then(function (result) {
        if (!result.ok) {
          showFlash(result.error || "That did not work.", false);
        } else if (action) {
          showFlash(result.message, true);
        } else {
          showFlash("Retry: " + String(result.status).replace(/_/g, " "), true);
        }
      })
      .catch(function (err) { showFlash("Request failed: " + err, false); })
      .then(function () {
        // Re-render either way; it replaces the (possibly disabled) button.
        // Only the queue fragment: a full dashboard refresh would also
        // re-render the Patient messages inbox and discard unsaved edits.
        refresh();
      });
  });

  // Refresh immediately when the tab is opened, then every REFRESH_MS while
  // it is both selected and the browser tab is visible.
  document.addEventListener("tabchange", function (event) {
    if (event.detail && event.detail.id === "queue") refresh();
  });

  setInterval(function () {
    if (!section.hidden && document.visibilityState === "visible") refresh();
  }, REFRESH_MS);
});
