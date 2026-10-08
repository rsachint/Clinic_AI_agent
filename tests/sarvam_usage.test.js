// Node tests for the pure helper of static/settings_sarvam.js (the Settings line about this month's Sarvam
// spend). No DOM, no network. Run: node tests/sarvam_usage.test.js (tests/test_planner_sarvam.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const usage = require("../static/settings_sarvam.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

test("money always shows two decimals", () => {
  assert.strictEqual(usage.rupees(12.4), "Rs 12.40");
  assert.strictEqual(usage.rupees(0), "Rs 0.00");
  assert.strictEqual(usage.rupees(undefined), "Rs 0.00");
  assert.strictEqual(usage.rupees(1234.5), "Rs 1234.50");
});
test("the line when it is on", () => {
  assert.deepStrictEqual(usage.usageText({ active: true, commands: 112, spend_rupees: 12.4, log_enabled: true }), {
    line: "Sarvam planner: Rs 12.40 this month (112 commands). Estimated from token counts at Sarvam's published prices.",
    note: "",
  });
});
test("one command is singular", () => {
  assert.ok(usage.usageText({ active: true, commands: 1, spend_rupees: 0.11 }).line.indexOf("(1 command)") > 0);
});
test("on with nothing used yet still shows the line", () => {
  assert.strictEqual(usage.usageText({ active: true, commands: 0, spend_rupees: 0 }).line,
    "Sarvam planner: Rs 0.00 this month (0 commands). Estimated from token counts at Sarvam's published prices.");
});
test("off and nothing used this month says it is off", () => {
  assert.deepStrictEqual(usage.usageText({ active: false, commands: 0, spend_rupees: 0 }), { line: "Sarvam planner is off.", note: "" });
});
test("off but used earlier this month shows the spend and says it is off now", () => {
  const text = usage.usageText({ active: false, commands: 5, spend_rupees: 0.55, log_enabled: true });
  assert.ok(text.line.indexOf("Rs 0.55 this month (5 commands)") > 0);
  assert.strictEqual(text.note, "Sarvam planner is off now; this is what it cost earlier this month.");
});
test("a switched-off planner log is called out", () => {
  const text = usage.usageText({ active: true, commands: 3, spend_rupees: 0.3, log_enabled: false });
  assert.strictEqual(text.note, "The planner log is switched off, so new commands are not being counted.");
});

if (failures.length) {
  console.log(failures.join("\n"));
  console.log(passed + " passed, " + failures.length + " FAILED");
  process.exit(1);
}
console.log(passed + " passed");
