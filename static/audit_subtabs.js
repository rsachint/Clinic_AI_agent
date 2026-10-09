// Sub-tabs inside the Audit log tab: Audit log, Unanswered questions and Connection. Same pattern as the
// Patients tab (static/patients_subtabs.js, whose two pure helpers are reused). The cards keep their ids
// (dashboard_refresh.js re-renders #audit-table-card; unanswered.js draws into #unanswered-card), so this only
// decides which one is shown. The chosen sub-tab is remembered in this browser (localStorage, in try/catch).
// Other scripts open one with window.AuditSubtabs.select("connection"); the DOM event "auditsubtabchange"
// ({ detail: { id } }) tells them when one opens.
(function () {
  if (typeof document === "undefined" || !document.addEventListener) return;

  function pick(stored, validIds, fallback) {
    if (window.pickSubtab) return window.pickSubtab(stored, validIds, fallback);
    return validIds.indexOf(stored) >= 0 ? stored : fallback;
  }
  function neighbour(validIds, current, key) {
    if (window.subtabNeighbour) return window.subtabNeighbour(validIds, current, key);
    var i = validIds.indexOf(current);
    if (i < 0) return null;
    if (key === "ArrowRight") return validIds[(i + 1) % validIds.length];
    if (key === "ArrowLeft") return validIds[(i - 1 + validIds.length) % validIds.length];
    return null;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var bar = document.getElementById("audit-subtabs");
    if (!bar) return;
    var section = bar.parentNode;

    var KEY = "clinic.auditSubtab";
    var links = {};
    var ids = [];
    Array.prototype.forEach.call(bar.querySelectorAll("[data-subtab-link]"), function (link) {
      var id = link.getAttribute("data-subtab-link");
      links[id] = link;
      ids.push(id);
    });
    var panels = {};
    Array.prototype.forEach.call(section.querySelectorAll(".subtab-panel[data-subtab]"), function (panel) {
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
      id = pick(id, ids, ids[0]);
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
      document.dispatchEvent(new CustomEvent("auditsubtabchange", { detail: { id: id } }));
    }

    ids.forEach(function (id) {
      links[id].addEventListener("click", function () { select(id); });
      links[id].addEventListener("keydown", function (event) {
        var next = neighbour(ids, current, event.key);
        if (next) { event.preventDefault(); select(next); links[next].focus(); }
      });
    });

    window.AuditSubtabs = { select: select, current: function () { return current; } };

    select(read(), false);
  });
})();
