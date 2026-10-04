// The database stores UTC; the clinic works in IST (UTC+05:30, no DST).
// window.formatIST("2026-10-03 06:34:16") -> "2026-10-03 12:04:16".
// Anything unparseable is returned unchanged.
window.formatIST = function (utc) {
  if (!utc) return utc;
  var d = new Date(String(utc).trim().replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(utc).trim()) ? "" : "Z"));
  if (isNaN(d.getTime())) return utc;
  return new Date(d.getTime() + 5.5 * 3600 * 1000).toISOString().slice(0, 19).replace("T", " ");
};
