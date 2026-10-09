// Node tests for the pure helpers of static/network_chip.js (the connection chip and its popover) and
// static/network_history.js (the Audit log -> Connection card). No DOM, no network.
// Run: node tests/network_chip.test.js (tests/test_network_health.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const chip = require("../static/network_chip.js");
const history = require("../static/network_history.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

test("the chip says exactly what the three states mean", () => {
  assert.deepStrictEqual(chip.chipView("good"), { label: "Connection good", cls: "net-good" });
  assert.deepStrictEqual(chip.chipView("slow"), { label: "Connection slow", cls: "net-slow" });
  assert.deepStrictEqual(chip.chipView("down"), { label: "No connection", cls: "net-down" });
});
test("an unknown or missing state reads as good", () => {
  assert.strictEqual(chip.chipView(undefined).label, "Connection good");
  assert.strictEqual(chip.chipView("weird").cls, "net-good");
});
test("a browser that is offline always shows No connection", () => {
  assert.strictEqual(chip.effectiveState("good", false), "down");
  assert.strictEqual(chip.effectiveState("slow", false), "down");
  assert.strictEqual(chip.effectiveState("slow", true), "slow");
  assert.strictEqual(chip.effectiveState("good", undefined), "good");
});
test("the popover title", () => {
  assert.strictEqual(chip.popoverTitle("good"), "Connection is good");
  assert.strictEqual(chip.popoverTitle("slow"), "Connection is unstable");
  assert.strictEqual(chip.popoverTitle("down"), "Connection is unstable");
});
test("the popover rows: name, status word and the failure ratio", () => {
  const lines = chip.serviceLines([
    { key: "voice", name: "Voice (microphone)", status: "ok", label: "OK", detail: null },
    { key: "planner", name: "Understanding commands", status: "slow", label: "Slow", detail: "2 of last 4 failed" },
    { key: "whatsapp", name: "WhatsApp messages", status: "down", label: "Not responding", detail: "2 of last 2 failed" },
  ]);
  assert.deepStrictEqual(lines, [
    { name: "Voice (microphone)", status: "OK", cls: "net-ok" },
    { name: "Understanding commands", status: "Slow · 2 of last 4 failed", cls: "net-slow" },
    { name: "WhatsApp messages", status: "Not responding · 2 of last 2 failed", cls: "net-down" },
  ]);
});
test("odd rows never break the popover", () => {
  assert.deepStrictEqual(chip.serviceLines(undefined), []);
  assert.deepStrictEqual(chip.serviceLines([{}]), [{ name: "", status: "OK", cls: "net-ok" }]);
});
test("last checked", () => {
  assert.strictEqual(chip.lastCheckedText("14:32"), "Last checked 14:32 IST");
  assert.strictEqual(chip.lastCheckedText(null), "Not checked yet");
  assert.strictEqual(chip.lastCheckedText(undefined), "Not checked yet");
});
test("how long ago, in plain words", () => {
  const now = 1000000 * 1000;
  assert.strictEqual(chip.agoText(1000000 - 20, now), "just now");
  assert.strictEqual(chip.agoText(1000000 - 300, now), "5 min ago");
  assert.strictEqual(chip.agoText(1000000 - 3 * 3600, now), "3 h ago");
  assert.strictEqual(chip.agoText(1000000 + 50, now), "just now");     // a slightly fast server clock
  assert.strictEqual(chip.agoText(null, now), "");
  assert.strictEqual(chip.agoText("x", now), "");
});
test("the Connection card's summary line", () => {
  assert.strictEqual(history.summaryText({ today_count: 3, state: "slow" }), "3 today · Now: Connection slow");
  assert.strictEqual(history.summaryText({ today_count: 0, state: "good" }), "0 today · Now: Connection good");
  assert.strictEqual(history.summaryText({ today_count: 12, state: "down" }), "12 today · Now: No connection");
  assert.strictEqual(history.summaryText({}), "0 today · Now: Connection good");
});
test("a problem row's four cells", () => {
  assert.deepStrictEqual(history.rowCells({ when: "2026-10-08 20:41:07", service: "Voice (microphone)", what: "Secure connection timed out", length: "10.0 s" }),
    ["2026-10-08 20:41:07", "Voice (microphone)", "Secure connection timed out", "10.0 s"]);
  assert.deepStrictEqual(history.rowCells({}), ["-", "-", "-", "-"]);
});
test("the Connection table's headings and empty text", () => {
  assert.deepStrictEqual(history.COLUMNS, ["When (IST)", "Service", "What happened", "Length"]);
  assert.strictEqual(history.EMPTY, "No connection problems recorded.");
});

if (failures.length) {
  console.log(failures.join("\n"));
  console.log(passed + " passed, " + failures.length + " FAILED");
  process.exit(1);
}
console.log(passed + " passed");
