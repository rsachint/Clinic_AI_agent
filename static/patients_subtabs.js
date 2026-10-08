// Sub-tabs inside the Patients tab: Patients, Missed follow-ups, Attendance today
// and Follow-ups. The cards keep their ids (dashboard_refresh.js and automation.js
// find them by id), so this only decides which one is shown. The chosen sub-tab is
// remembered in this browser (localStorage, wrapped in try/catch: it can be blocked).
//
// The pure helper at the top is exported for tests/ptt_client.test.js.

// Which sub-tab a stored value means: a valid id, else the fallback.
window.pickSubtab = function (stored, validIds, fallback) {
  return validIds.indexOf(stored) >= 0 ? stored : fallback;
};

// The next / previous sub-tab for an arrow key (wraps round), else null.
window.subtabNeighbour = function (validIds, current, key) {
  var i = validIds.indexOf(current);
  if (i < 0) return null;
  if (key === "ArrowRight") return validIds[(i + 1) % validIds.length];
  if (key === "ArrowLeft") return validIds[(i - 1 + validIds.length) % validIds.length];
  return null;
};

document.addEventListener("DOMContentLoaded", function () {
  var bar = document.getElementById("patients-subtabs");
  if (!bar) return;

  var KEY = "clinic.patientsSubtab";
  var links = {};
  var ids = [];
  Array.prototype.forEach.call(bar.querySelectorAll("[data-subtab-link]"), function (link) {
    var id = link.getAttribute("data-subtab-link");
    links[id] = link;
    ids.push(id);
  });
  var panels = {};
  Array.prototype.forEach.call(document.querySelectorAll(".subtab-panel[data-subtab]"), function (panel) {
    panels[panel.getAttribute("data-subtab")] = panel;
  });

  function read() {
    try { return window.localStorage ? window.localStorage.getItem(KEY) : null; } catch (e) { return null; }
  }
  function write(value) {
    try { if (window.localStorage) window.localStorage.setItem(KEY, value); } catch (e) {}
  }

  var current = null;
  function select(id, remember) {
    id = window.pickSubtab(id, ids, ids[0]);
    current = id;
    ids.forEach(function (name) {
      var active = name === id;
      if (panels[name]) panels[name].hidden = !active;
      if (links[name]) {
        links[name].classList.toggle("active", active);
        links[name].setAttribute("aria-selected", active ? "true" : "false");
        links[name].tabIndex = active ? 0 : -1;
      }
    });
    if (remember !== false) write(id);
    document.dispatchEvent(new CustomEvent("subtabchange", { detail: { id: id } }));
  }

  ids.forEach(function (id) {
    links[id].addEventListener("click", function () { select(id); });
    links[id].addEventListener("keydown", function (event) {
      var next = window.subtabNeighbour(ids, current, event.key);
      if (next) { event.preventDefault(); select(next); links[next].focus(); }
    });
  });

  // Lets other scripts (the voice assistant, tests) open one.
  window.PatientsSubtabs = { select: select, current: function () { return current; } };

  select(read(), false);
});

if (typeof module !== "undefined" && module.exports) {
  module.exports = { pickSubtab: window.pickSubtab, subtabNeighbour: window.subtabNeighbour };
}
