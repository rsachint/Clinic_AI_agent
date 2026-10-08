// Node tests for the pure helper of static/booking_phone.js (the "a phone number is required to book" rule the review
// card and the staff form use; the server's own rule is clinic/booking_phone.py). No DOM, no network.
// Run: node tests/booking_phone.test.js (tests/test_booking_phone_required.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const bp = require("../static/booking_phone.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

const PATIENTS = [
  { id: 1, name: "Sunita Devi", phone: "9876543210" },
  { id: 2, name: "Anita Rao", phone: "12345" },
  { id: 3, name: "Blank", phone: "" },
  { id: 4, name: "Formatted", phone: "+91 98765 43211" },
];

test("the forms that normalise to ten digits are valid", () => {
  ["9876500301", "+91 98765 00301", "09876500301", "98765 00301", "919876500301", "98765-00301", 9876500301, "1122334455"]
    .forEach((p) => assert.strictEqual(bp.valid(p).length, 10, String(p)));
  assert.strictEqual(bp.valid("+91 98765 00301"), "9876500301");
});

test("blank, short and non-numeric are not valid", () => {
  [null, undefined, "", "   ", "12345", "98765 0030", "abcdefghij"].forEach((p) => assert.strictEqual(bp.valid(p), "", String(p)));
});

test("a new person needs a phone on the booking", () => {
  assert.strictEqual(bp.problem({ patient_id: null, patient_phone: "" }, PATIENTS), bp.REQUIRED);
  assert.strictEqual(bp.problem({ patient_phone: "98765" }, PATIENTS), bp.REQUIRED);
  assert.strictEqual(bp.problem({}, PATIENTS), bp.REQUIRED);
  assert.strictEqual(bp.problem({ patient_id: "", patient_phone: "+91 98765 00301" }, PATIENTS), null);
});

test("a registered patient with a valid number needs nothing more", () => {
  assert.strictEqual(bp.problem({ patient_id: 1 }, PATIENTS), null);
  assert.strictEqual(bp.problem({ patient_id: "4", patient_phone: "" }, PATIENTS), null);
  assert.strictEqual(bp.problem({ patient_id: 1, patient_phone: "12" }, PATIENTS), null);    // their own number counts
});

test("a registered patient with no valid number needs one on the booking", () => {
  [2, 3].forEach((id) => {
    assert.strictEqual(bp.problem({ patient_id: id }, PATIENTS), bp.PATIENT_NO_PHONE);
    assert.strictEqual(bp.problem({ patient_id: id, patient_phone: "123" }, PATIENTS), bp.PATIENT_NO_PHONE);
    assert.strictEqual(bp.problem({ patient_id: id, patient_phone: "9876500302" }, PATIENTS), null);
  });
});

test("a patient the page does not know is left to the server", () => {
  assert.strictEqual(bp.problem({ patient_id: 99 }, PATIENTS), null);
  assert.strictEqual(bp.required({ patient_id: 99 }, PATIENTS), false);
});

test("the phone is required when no registered patient (or one without a number) is picked", () => {
  assert.strictEqual(bp.required({}, PATIENTS), true);
  assert.strictEqual(bp.required({ patient_id: null }, PATIENTS), true);
  assert.strictEqual(bp.required({ patient_id: 1 }, PATIENTS), false);
  assert.strictEqual(bp.required({ patient_id: 2 }, PATIENTS), true);
  assert.strictEqual(bp.required({ patient_id: "3" }, PATIENTS), true);
});

test("the wording is the server's (a single line each, so a text search finds it)", () => {
  assert.strictEqual(bp.REQUIRED, "A phone number is required to book an appointment. Enter the patient's 10-digit phone number.");
  assert.ok(bp.PATIENT_NO_PHONE.indexOf("no valid phone number on file") > 0);
});

if (failures.length) {
  console.log(failures.join("\n"));
  console.log(passed + " passed, " + failures.length + " FAILED");
  process.exit(1);
}
console.log(passed + " passed");
