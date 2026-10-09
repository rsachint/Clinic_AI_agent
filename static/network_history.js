// Audit log -> Connection: the "Connection problems" card. A line "N today · Now: <state>" and a table of the
// last 50 network failures the app noticed (GET /network/incidents; clinic/network_health.py): When (IST),
// Service, What happened (plain words, never an error text, host or key) and Length (seconds the call waited).
// Loaded when the Connection sub-tab opens, and again when a `networkstatus` update arrives while it is open.
// Text is always put in with textContent, never as HTML.
//
// The pure helpers at the top are exported for tests/network_chip.test.js.
(function () {
  var NOW_WORDS = { good: "Connection good", slow: "Connection slow", down: "No connection" };

  // "3 today · Now: Connection good"
  function summaryText(data) {
    var count = Number(data && data.today_count) || 0;
    return count + " today · Now: " + (NOW_WORDS[data && data.state] || NOW_WORDS.good);
  }

  // One table row's four cells, in column order.
  function rowCells(row) {
    return [String(row.when || "-"), String(row.service || "-"), String(row.what || "-"), String(row.length || "-")];
  }

  var COLUMNS = ["When (IST)", "Service", "What happened", "Length"];
  var EMPTY = "No connection problems recorded.";

  var api = { summaryText: summaryText, rowCells: rowCells, COLUMNS: COLUMNS, EMPTY: EMPTY };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  window.NetworkHistory = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var summary = document.getElementById("network-summary");
    var holder = document.getElementById("network-table");
    var panel = document.getElementById("network-card");
    if (!summary || !holder || !panel) return;
    var latest = 0;                 // only the newest request may draw

    function el(tag, text) {
      var node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      return node;
    }
    function draw(data) {
      summary.textContent = summaryText(data);
      while (holder.firstChild) holder.removeChild(holder.firstChild);
      var rows = Array.isArray(data.rows) ? data.rows : [];
      if (!rows.length) {
        var empty = el("p", EMPTY);
        empty.className = "muted";
        holder.appendChild(empty);
        return;
      }
      var table = el("table");
      table.id = "network-table-rows";
      var head = el("tr");
      COLUMNS.forEach(function (name) { head.appendChild(el("th", name)); });
      table.appendChild(head);
      rows.forEach(function (row) {
        var tr = el("tr");
        rowCells(row).forEach(function (text) { tr.appendChild(el("td", text)); });
        table.appendChild(tr);
      });
      holder.appendChild(table);
    }
    function load() {
      var mine = ++latest;
      return fetch("/network/incidents").then(function (r) { return r.json(); })
        .then(function (r) { if (r && r.ok && mine === latest) draw(r); })
        .catch(function () { /* the card keeps what it had */ });
    }
    function visible() {
      var auditOpen = !!document.querySelector('.tab-panel[data-tab="audit"]:not([hidden])');
      return auditOpen && !panel.parentNode.hidden;
    }

    document.addEventListener("auditsubtabchange", function (event) {
      if (event.detail && event.detail.id === "connection" && visible()) load();
    });
    document.addEventListener("tabchange", function (event) {
      if (event.detail && event.detail.id === "audit" && visible()) load();
    });
    document.addEventListener("networkstatus", function () { if (visible()) load(); });
  });
})();
