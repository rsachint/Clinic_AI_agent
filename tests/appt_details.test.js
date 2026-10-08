// Node tests for static/appt_details.js: the appointment lines shared by the calendar hover and the
// Cancel / Reschedule review card. No DOM, no network. Run: node tests/appt_details.test.js
// (tests/test_appointment_card_details.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const ad = require("../static/appt_details.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

const FULL = {
  appt_date: "2026-10-12", start_time: "09:30", end_time: "10:00", patient_name: "Amit Dua", token: "B-T04",
  branch: "Branch B", doctor: "Dr. Rao", patient_phone: "9876500301", status: "no_show",
};

test("day label and time range", () => {
  assert.strictEqual(ad.dayLabel("2026-10-12"), "Mon 12 Oct");
  assert.strictEqual(ad.dayLabel("2026-01-04"), "Sun 4 Jan");
  assert.strictEqual(ad.timeRange(FULL), "09:30-10:00");
  assert.strictEqual(ad.timeRange({ start_time: "09:30" }), "09:30");
  assert.strictEqual(ad.timeRange({}), "");
});

test("a full appointment gives the hover's lines in order", () => {
  const lines = ad.lines(FULL);
  assert.deepStrictEqual(lines.map((l) => l.kind), ["name", "when", "place", "phone", "status"]);
  assert.deepStrictEqual(lines.map((l) => l.text), [
    "B-T04 · Amit Dua", "Mon 12 Oct · 09:30-10:00", "Branch B · Dr. Rao", "9876500301", "Status: no show",
  ]);
});

test("the wording is the calendar's own (name, 'Day D Mon · start-end', 'Branch · Doctor', phone, 'Status: x')", () => {
  const t = ad.lines({ appt_date: "2026-10-12", start_time: "09:30", end_time: "10:00", patient_name: "A", branch: "B", status: "booked" });
  assert.strictEqual(t[1].text, "Mon 12 Oct · 09:30-10:00");
  assert.strictEqual(t[2].text, "B");                       // no doctor: just the branch
  assert.strictEqual(t[3].text, "Status: booked");
});

test("a server option names the patient in `who` or `patient_name`", () => {
  assert.strictEqual(ad.lines({ who: "Sunita Walk", appt_date: "2026-10-12", start_time: "15:00" })[0].text, "Sunita Walk");
  assert.strictEqual(ad.lines({ patient_name: "Amit Dua" })[0].text, "Amit Dua");
});

test("missing fields leave their line out; a nameless one says so", () => {
  const lines = ad.lines({ appt_date: "2026-10-12", start_time: "15:00" });
  assert.deepStrictEqual(lines.map((l) => l.kind), ["name", "when"]);
  assert.strictEqual(lines[0].text, "(no name)");
  assert.strictEqual(lines[1].text, "Mon 12 Oct · 15:00");
  assert.deepStrictEqual(ad.lines({ branch: null, doctor: "Dr. Rao", patient_phone: "", status: null }).map((l) => l.kind), ["name"]);
  assert.deepStrictEqual(ad.lines().map((l) => l.kind), ["name"]);
});

test("the dropdown label is the server's, else date, time and name", () => {
  assert.strictEqual(ad.optionLabel({ label: "2026-10-12 09:30 - Amit Dua (…1234)", appt_date: "x" }), "2026-10-12 09:30 - Amit Dua (…1234)");
  assert.strictEqual(ad.optionLabel({ appt_date: "2026-10-12", start_time: "09:30", patient_name: "Amit Dua" }), "2026-10-12 09:30 - Amit Dua");
  assert.strictEqual(ad.optionLabel({ appt_date: "2026-10-12", start_time: "09:30" }), "2026-10-12 09:30");
});

if (failures.length) {
  console.log(failures.join("\n"));
  console.log(passed + " passed, " + failures.length + " FAILED");
  process.exit(1);
}
console.log(passed + " passed");
