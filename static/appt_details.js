// One appointment as lines of text, the same on the calendar's hover card (static/calendar_view.js) and on the
// Cancel / Reschedule review card (static/review_card.js), so the two never drift apart. Pure and DOM-free:
// tests/appt_details.test.js runs it under node.
//
//   ApptDetails.dayLabel("2026-10-12")     "Mon 12 Oct"
//   ApptDetails.timeRange(a)               "09:30-10:00" (just the start when there is no end)
//   ApptDetails.lines(a)                   [{kind, text}] for kind name | when | place | phone | status; a line with
//                                          nothing to say is left out
//   ApptDetails.optionLabel(a)             one line for a dropdown: "2026-10-12 09:30 - Amit Dua (...1234)"
//
// `a` is a calendar row or a review-card option: appt_date, start_time, end_time, patient_name (or who), token,
// branch, doctor, patient_phone, status. Notes and diagnoses are never part of it.

(function () {
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function dayLabel(iso) {
    var p = String(iso || "").split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    if (isNaN(d.getTime())) return String(iso || "");
    return DAYS[(d.getUTCDay() + 6) % 7] + " " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()];
  }

  function timeRange(a) {
    if (!a || !a.start_time) return "";
    return a.end_time ? a.start_time + "-" + a.end_time : String(a.start_time);
  }

  function nameOf(a) { return a.patient_name || a.who || ""; }

  function lines(a) {
    a = a || {};
    var out = [];
    out.push({ kind: "name", text: (a.token ? a.token + " · " : "") + (nameOf(a) || "(no name)") });
    var when = [a.appt_date ? dayLabel(a.appt_date) : "", timeRange(a)].filter(Boolean).join(" · ");
    if (when) out.push({ kind: "when", text: when });
    if (a.branch) out.push({ kind: "place", text: a.branch + (a.doctor ? " · " + a.doctor : "") });
    if (a.patient_phone) out.push({ kind: "phone", text: String(a.patient_phone) });
    if (a.status) out.push({ kind: "status", text: "Status: " + String(a.status).replace("_", " ") });
    return out;
  }

  function optionLabel(a) {
    if (a.label) return a.label;
    return [a.appt_date, a.start_time].filter(Boolean).join(" ") + (nameOf(a) ? " - " + nameOf(a) : "");
  }

  var api = { dayLabel: dayLabel, timeRange: timeRange, lines: lines, optionLabel: optionLabel };
  if (typeof window !== "undefined") window.ApptDetails = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
