// Node tests for the pure helpers of static/settings_collapse.js (Settings: collapsible configuration cards).
// No DOM, no network. Run: node tests/settings_collapse.test.js (tests/test_settings_collapse.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const c = require("../static/settings_collapse.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

test("a card is remembered under its heading text, trimmed, lower-cased, spaces collapsed", () => {
  assert.strictEqual(c.cardKey("  Doctor   schedules "), "doctor schedules");
  assert.strictEqual(c.cardKey("WhatsApp message templates for reminders"), "whatsapp message templates for reminders");
  assert.strictEqual(c.cardKey(null), "");
});
test("unreadable or wrong-shaped stored text is an empty map", () => {
  assert.deepStrictEqual(c.readState(null), {});
  assert.deepStrictEqual(c.readState("not json"), {});
  assert.deepStrictEqual(c.readState("[1,2]"), {});
  assert.deepStrictEqual(c.readState("\"x\""), {});
});
test("a stored map is read back", () => {
  assert.deepStrictEqual(c.readState('{"doctors":true,"branches":false}'), { doctors: true, branches: false });
});
test("a card starts collapsed and is open only when it was opened on purpose", () => {
  assert.strictEqual(c.isOpen({}, "doctors"), false);
  assert.strictEqual(c.isOpen({ doctors: false }, "doctors"), false);
  assert.strictEqual(c.isOpen({ doctors: true }, "doctors"), true);
  assert.strictEqual(c.isOpen(null, "doctors"), false);
  assert.strictEqual(c.isOpen({ doctors: "yes" }, "doctors"), false);
});
test("setting one card leaves the others and does not change the original", () => {
  const before = { doctors: true };
  const after = c.withOpen(before, "branches", true);
  assert.deepStrictEqual(after, { doctors: true, branches: true });
  assert.deepStrictEqual(before, { doctors: true });
  assert.deepStrictEqual(c.withOpen(after, "doctors", false), { doctors: false, branches: true });
});

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(passed + " passed");
