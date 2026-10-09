// Node test for the pure helpers of static/read_format.js (how a cell of the Assistant's read tables is shown).
// No DOM, no network. Run: node tests/read_format.test.js (tests/test_next_available.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const fmt = require("../static/read_format.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

test("money columns keep their Indian grouping", () => {
  assert.strictEqual(fmt.rupees(124000.5), "Rs 1,24,000.50");
  assert.deepStrictEqual(fmt.cell("fee_rupees", 12400), { text: "Rs 12,400", numeric: true });
});

test("a plain value is shown as it came", () => {
  assert.deepStrictEqual(fmt.cell("name", "Amit"), { text: "Amit", numeric: false });
  assert.deepStrictEqual(fmt.cell("age", 41), { text: "41", numeric: true });
  assert.deepStrictEqual(fmt.cell("age", null), { text: "-", numeric: false });
});

test("a list of free slots comes back as separate pieces, not one comma-run line", () => {
  const slots = ["16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00", "19:30"];
  const shown = fmt.cell("slots", slots);
  assert.deepStrictEqual(shown.chips, slots);
  assert.strictEqual(shown.numeric, false);
  assert.strictEqual(shown.text, slots.join(", "));
});

test("a long list keeps every slot, in order (nothing is cut for width)", () => {
  const slots = [];
  for (let h = 9; h < 20; h++) { slots.push(("0" + h).slice(-2) + ":00"); slots.push(("0" + h).slice(-2) + ":30"); }
  assert.strictEqual(fmt.chips("slots", slots).length, slots.length);
  assert.strictEqual(fmt.chips("slots", slots)[0], "09:00");
  assert.strictEqual(fmt.chips("slots", slots)[slots.length - 1], "19:30");
});

test("empty and blank entries are dropped; an empty list is not chips", () => {
  assert.deepStrictEqual(fmt.chips("slots", ["09:00", "", null, "09:30"]), ["09:00", "09:30"]);
  assert.strictEqual(fmt.chips("slots", []), null);
  assert.strictEqual(fmt.cell("slots", []).chips, undefined);
});

test("the times text of an all-branches answer is split too, and keeps its 'more' mark", () => {
  assert.deepStrictEqual(fmt.chips("times", "09:00, 09:30, 10:00"), ["09:00", "09:30", "10:00"]);
  assert.deepStrictEqual(fmt.chips("times", "09:00, 09:30 ..."), ["09:00", "09:30", "…"]);
  assert.strictEqual(fmt.chips("times", "09:00"), null);
});

test("only a list or the times column is split: a sentence with commas stays text", () => {
  assert.strictEqual(fmt.chips("notes", "bring the report, come early"), null);
  assert.deepStrictEqual(fmt.cell("notes", "bring the report, come early"), { text: "bring the report, come early", numeric: false });
});

test("numbers inside a list are shown as text pieces", () => {
  assert.deepStrictEqual(fmt.chips("tokens", [1, 2, 3]), ["1", "2", "3"]);
});

if (failures.length) {
  console.log(failures.join("\n"));
  console.log(passed + " passed, " + failures.length + " failed");
  process.exit(1);
}
console.log(passed + " passed");
