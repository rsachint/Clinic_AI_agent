// How a table cell of a read answer is shown (the Assistant's tables, see live_voice.js):
// money columns (a name ending in "_rupees") read "Rs 12,400" with Indian digit grouping,
// numbers line up on the right, everything else is shown as it came. Text only: the value
// is put in with textContent by the caller, never as HTML.
//
//   ReadFormat.rupees(12400)            -> "Rs 12,400"
//   ReadFormat.isNumericColumn(key, rows)
//   ReadFormat.cell(key, value)         -> { text, numeric, chips? }
//   ReadFormat.chips(key, value)        -> ["09:00", "09:30"] | null
//
// A list of short values (the free slots of a day: ["16:00", "16:30", ...]) is NOT run together as one
// comma-separated line that the table clips at the card's edge: `cell` hands it back as `chips`, and the caller
// shows each one as its own pill that wraps with the others inside the cell (textContent, never HTML).
window.ReadFormat = (function () {
  function rupees(value) {
    var n = Math.round(Math.abs(Number(value)) * 100);
    var whole = Math.floor(n / 100);
    var paise = n % 100;
    var digits = String(whole);
    if (digits.length > 3) {
      var head = digits.slice(0, -3);
      var groups = [];
      while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) groups.unshift(head);
      digits = groups.concat([digits.slice(-3)]).join(",");
    }
    return "Rs " + (Number(value) < 0 ? "-" : "") + digits + (paise ? "." + (paise < 10 ? "0" : "") + paise : "");
  }

  function isMoneyColumn(key) { return /_rupees$/.test(String(key)); }

  function isNumber(value) { return typeof value === "number" && isFinite(value); }

  function isNumericColumn(key, rows) {
    if (isMoneyColumn(key)) return true;
    var seen = false;
    for (var i = 0; i < (rows || []).length; i++) {
      var value = rows[i] && rows[i][key];
      if (value === null || value === undefined) continue;
      if (!isNumber(value)) return false;
      seen = true;
    }
    return seen;
  }

  // The pieces of a cell that is a list: a real array, or the "times" text of an all-branches answer
  // ("09:00, 09:30, 10:00 ..."; a trailing " ..." means more were left out and stays as its own piece).
  function chips(key, value) {
    if (Array.isArray(value)) {
      var items = value.filter(function (v) { return v !== null && v !== undefined && String(v) !== ""; }).map(String);
      return items.length ? items : null;
    }
    if (String(key).toLowerCase() === "times" && typeof value === "string" && value.indexOf(",") !== -1) {
      var more = /\s*\.\.\.\s*$/.test(value);
      var parts = value.replace(/\s*\.\.\.\s*$/, "").split(/\s*,\s*/).filter(Boolean);
      if (more) parts.push("\u2026");
      return parts.length ? parts : null;
    }
    return null;
  }

  function cell(key, value) {
    if (value === null || value === undefined) return { text: "-", numeric: false };
    var pieces = chips(key, value);
    if (pieces) return { text: pieces.join(", "), numeric: false, chips: pieces };
    if (isMoneyColumn(key) && isNumber(value)) return { text: rupees(value), numeric: true };
    return { text: String(value), numeric: isNumber(value) };
  }

  return { rupees: rupees, isNumericColumn: isNumericColumn, cell: cell, chips: chips };
})();

if (typeof module !== "undefined" && module.exports) module.exports = window.ReadFormat;
