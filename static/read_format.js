// How a table cell of a read answer is shown (the Assistant's tables, see live_voice.js):
// money columns (a name ending in "_rupees") read "Rs 12,400" with Indian digit grouping,
// numbers line up on the right, everything else is shown as it came. Text only: the value
// is put in with textContent by the caller, never as HTML.
//
//   ReadFormat.rupees(12400)            -> "Rs 12,400"
//   ReadFormat.isNumericColumn(key, rows)
//   ReadFormat.cell(key, value)         -> { text, numeric }
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

  function cell(key, value) {
    if (value === null || value === undefined) return { text: "-", numeric: false };
    if (isMoneyColumn(key) && isNumber(value)) return { text: rupees(value), numeric: true };
    return { text: String(value), numeric: isNumber(value) };
  }

  return { rupees: rupees, isNumericColumn: isNumericColumn, cell: cell };
})();

if (typeof module !== "undefined" && module.exports) module.exports = window.ReadFormat;
