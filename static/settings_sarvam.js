// Settings tab: one line saying what the Sarvam planner (the hosted model that understands staff
// commands, PLANNER_BACKEND=sarvam) has cost this calendar month. An estimate from token counts at
// Sarvam's published prices, read from GET /settings/sarvam-usage; nothing to save. Refreshed when
// the Settings tab is opened. Text only, never HTML.
(function () {
  function rupees(value) { return "Rs " + Number(value || 0).toFixed(2); }

  // The one line (and, when needed, a second note) for a /settings/sarvam-usage reply.
  function usageText(data) {
    var commands = Number(data.commands) || 0;
    if (!data.active && !commands) {
      return { line: "Sarvam planner is off.", note: "" };
    }
    var line = "Sarvam planner: " + rupees(data.spend_rupees) + " this month (" + commands + (commands === 1 ? " command" : " commands") + "). Estimated from token counts at Sarvam's published prices.";
    var note = "";
    if (data.log_enabled === false) note = "The planner log is switched off, so new commands are not being counted.";
    else if (!data.active) note = "Sarvam planner is off now; this is what it cost earlier this month.";
    return { line: line, note: note };
  }
  var api = { usageText: usageText, rupees: rupees };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  window.SarvamUsage = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("settings-sarvam");
    if (!root) return;

    function draw(data) {
      var text = usageText(data);
      while (root.firstChild) root.removeChild(root.firstChild);
      var card = document.createElement("div");
      card.className = "card";
      card.id = "settings-sarvam-usage";
      var line = document.createElement("p");
      line.className = "sarvam-usage-line";
      line.textContent = text.line;
      card.appendChild(line);
      if (text.note) {
        var note = document.createElement("p");
        note.className = "muted";
        note.textContent = text.note;
        card.appendChild(note);
      }
      root.appendChild(card);
    }
    function load() {
      return fetch("/settings/sarvam-usage").then(function (r) { return r.json(); })
        .then(function (r) { if (r.ok) draw(r.data); })
        .catch(function () { /* nothing to show */ });
    }
    document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "settings") load(); });
    load();
  });
})();
