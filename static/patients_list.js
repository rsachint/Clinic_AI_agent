// Patients table (Patients tab): shows the 10 newest patients, and a down arrow
// after the last row loads the next 10, again and again until everyone is shown.
//
// Paging is by id ("older than the last row shown") rather than by offset, so a
// patient registered while the table is open can never push a row onto the next
// page twice. After an approval the dashboard re-renders this card back to the
// first 10 (static/dashboard_refresh.js); it asks us to put the rows back.
// Pure helpers are at the top so tests/ptt_client.test.js can run them in node.

window.patientsCountText = function (shown, total) {
  return "(showing " + shown + " of " + total + ")";
};

// Rows from the server that the table does not already show, in order.
window.patientsNewRows = function (shownIds, rows) {
  var seen = {};
  shownIds.forEach(function (id) { seen[String(id)] = true; });
  return (rows || []).filter(function (row) {
    if (seen[String(row.id)]) return false;
    seen[String(row.id)] = true;
    return true;
  });
};

window.PatientsList = (function () {
  var busy = false;

  function card() { return document.getElementById("patients-table-card"); }
  function table() { return document.getElementById("patients-table"); }
  function patientRows() {
    var t = table();
    return t ? Array.prototype.slice.call(t.querySelectorAll("tr[data-patient-id]")) : [];
  }
  function shown() { return patientRows().length; }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text;
    return td;
  }
  function buildRow(p) {
    var tr = document.createElement("tr");
    tr.className = "patient-row";
    tr.setAttribute("data-patient-id", String(p.id));
    tr.setAttribute("tabindex", "0");
    tr.setAttribute("title", "Show appointment activity");
    tr.appendChild(cell(p.name));
    tr.appendChild(cell(p.phone));
    tr.appendChild(cell(p.age || "-"));
    tr.appendChild(cell(p.registered_at || ""));
    return tr;
  }

  function setMessage(text) {
    var button = document.getElementById("patients-more");
    if (!button) return;
    var label = button.querySelector("span");
    if (label) label.textContent = text;
  }

  function load(limit) {
    var t = table();
    var rows = patientRows();
    if (!t || !rows.length || busy) return Promise.resolve();
    busy = true;
    var button = document.getElementById("patients-more");
    if (button) button.disabled = true;
    setMessage("Loading...");
    var lastId = rows[rows.length - 1].getAttribute("data-patient-id");
    return fetch("/patients/page?before_id=" + encodeURIComponent(lastId) + "&limit=" + encodeURIComponent(limit || 10))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "failed");
        var ids = patientRows().map(function (row) { return row.getAttribute("data-patient-id"); });
        var moreRow = t.querySelector(".patients-more-row");
        var parent = moreRow ? moreRow.parentNode : (t.querySelector("tbody") || t);
        window.patientsNewRows(ids, data.patients).forEach(function (p) {
          var tr = buildRow(p);
          if (moreRow) parent.insertBefore(tr, moreRow); else parent.appendChild(tr);
        });
        var count = document.getElementById("patients-count");
        if (count) count.textContent = window.patientsCountText(shown(), count.getAttribute("data-total") || shown());
        if (!data.has_more && moreRow) moreRow.remove();
      })
      .catch(function (err) {
        setMessage("Could not load - try again");
        if (window.console) console.warn("patients page failed: " + err.message);
      })
      .then(function () {
        busy = false;
        var again = document.getElementById("patients-more");
        if (again) {
          again.disabled = false;
          if (again.textContent.indexOf("Loading") !== -1) setMessage("Next 10");
        }
      });
  }

  // The dashboard re-rendered the card to its first 10: show the rows that were open again.
  function restore(count) {
    var missing = count - shown();
    if (missing > 0) return load(missing);
    return Promise.resolve();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var c = card();
    if (!c) return;
    c.addEventListener("click", function (event) {
      if (event.target.closest("#patients-more")) load(10);
    });
  });

  return { shown: shown, load: load, restore: restore };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { patientsCountText: window.patientsCountText, patientsNewRows: window.patientsNewRows };
}
