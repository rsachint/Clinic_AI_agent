// Node tests for the pure helpers of static/unanswered.js (the Audit tab card), static/unanswered_notice.js
// (the one-time "it works now" line) and static/read_format.js (money and numbers in the Assistant's tables).
// No DOM, no network. Run: node tests/unanswered_ui.test.js (tests/test_unanswered_ui.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const card = require("../static/unanswered.js");
const notice = require("../static/unanswered_notice.js");
const fmt = require("../static/read_format.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

test("each status offers the right buttons", () => {
  assert.deepStrictEqual(card.actionsFor("new"), ["resolved", "building", "dismissed"]);
  assert.deepStrictEqual(card.actionsFor("building"), ["resolved", "dismissed"]);
  assert.deepStrictEqual(card.actionsFor("resolved"), ["new"]);
  assert.deepStrictEqual(card.actionsFor("dismissed"), ["new"]);
});
test("button labels", () => {
  assert.strictEqual(card.actionLabel("resolved"), "Mark as added");
  assert.strictEqual(card.actionLabel("building"), "Mark as building");
  assert.strictEqual(card.actionLabel("dismissed"), "Dismiss");
  assert.strictEqual(card.actionLabel("new"), "Reopen");
});
test("how many times it was asked", () => {
  assert.strictEqual(card.askedText({ times_asked: 1 }), "once");
  assert.strictEqual(card.askedText({ times_asked: 4 }), "4 times");
});
test("status words", () => {
  assert.strictEqual(card.statusLabel("resolved"), "Added");
  assert.strictEqual(card.statusLabel("building"), "Being built");
});
test("the notice wording is fixed and drops a trailing full stop", () => {
  assert.strictEqual(notice.noticeText({ question: "who is on duty now", note: "Ask: who is on duty now." }),
    "You asked 'who is on duty now' earlier. It works now: Ask: who is on duty now. Try it again.");
  assert.strictEqual(notice.noticeText({ question: "q", note: "Ask: x" }), "You asked 'q' earlier. It works now: Ask: x. Try it again.");
});
test("money reads Rs with Indian grouping", () => {
  const cases = [[12400, "Rs 12,400"], [0, "Rs 0"], [999.5, "Rs 999.50"], [124000, "Rs 1,24,000"], [12400000, "Rs 1,24,00,000"],
    [1234567.89, "Rs 12,34,567.89"], [0.05, "Rs 0.05"], [-500, "Rs -500"], [1500.5, "Rs 1,500.50"]];
  cases.forEach(([n, text]) => assert.strictEqual(fmt.rupees(n), text, String(n)));
});
test("cells: money, numbers, text, empty", () => {
  assert.deepStrictEqual(fmt.cell("amount_rupees", 1200), { text: "Rs 1,200", numeric: true });
  assert.deepStrictEqual(fmt.cell("total_fee_rupees", 1500.5), { text: "Rs 1,500.50", numeric: true });
  assert.deepStrictEqual(fmt.cell("count", 4), { text: "4", numeric: true });
  assert.deepStrictEqual(fmt.cell("name", "Amit"), { text: "Amit", numeric: false });
  assert.deepStrictEqual(fmt.cell("date", "2026-10-07"), { text: "2026-10-07", numeric: false });
  assert.deepStrictEqual(fmt.cell("phone", null), { text: "-", numeric: false });
  assert.deepStrictEqual(fmt.cell("fee_rupees", null), { text: "-", numeric: false });
});
test("a column is numeric when every value is a number (or it is money)", () => {
  assert.ok(fmt.isNumericColumn("count", [{ count: 1 }, { count: 2 }, { count: null }]));
  assert.ok(!fmt.isNumericColumn("name", [{ name: "a" }]));
  assert.ok(!fmt.isNumericColumn("age", [{ age: 3 }, { age: "n/a" }]));
  assert.ok(fmt.isNumericColumn("fee_rupees", []));
  assert.ok(!fmt.isNumericColumn("count", []));
});
test("a text value is never turned into markup", () => {
  assert.strictEqual(fmt.cell("name", "<b>x</b>").text, "<b>x</b>");
});

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("ok " + passed + " passed");
