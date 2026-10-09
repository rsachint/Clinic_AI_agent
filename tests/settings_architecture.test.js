// Node tests for the pure helpers of static/settings_architecture.js (Settings -> Command understanding).
// No DOM, no network. Run: node tests/settings_architecture.test.js (tests/test_architecture_switch.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const arch = require("../static/settings_architecture.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

test("the current mode line names the mode", () => {
  assert.strictEqual(arch.currentLine({ label: "New (model first, experimental)" }), "Current mode: New (model first, experimental)");
  assert.strictEqual(arch.currentLine({ label: "Classic (rules first)" }), "Current mode: Classic (rules first)");
});
test("with no data the line says Classic, the safe direction", () => {
  assert.strictEqual(arch.currentLine(null), "Current mode: Classic (rules first)");
  assert.strictEqual(arch.currentLine({}), "Current mode: Classic (rules first)");
});
test("the chosen mode is the ticked radio", () => {
  assert.strictEqual(arch.chosenMode([{ value: "classic", checked: false }, { value: "model_first", checked: true }]), "model_first");
  assert.strictEqual(arch.chosenMode([{ value: "classic", checked: true }, { value: "model_first", checked: false }]), "classic");
});
test("the third option can be chosen and named", () => {
  const radios = [{ value: "classic", checked: false }, { value: "model_first", checked: false }, { value: "model_reads", checked: true }];
  assert.strictEqual(arch.chosenMode(radios), "model_reads");
  assert.strictEqual(arch.currentLine({ label: "Model does all read operations" }), "Current mode: Model does all read operations");
});
test("an option's help sentence is shown only when the server sent one", () => {
  assert.strictEqual(arch.optionHelp({ value: "model_reads", help: "Like the New mode." }), "Like the New mode.");
  assert.strictEqual(arch.optionHelp({ value: "classic" }), "");
  assert.strictEqual(arch.optionHelp({ value: "classic", help: 5 }), "");
  assert.strictEqual(arch.optionHelp(null), "");
});
test("nothing ticked chooses nothing", () => {
  assert.strictEqual(arch.chosenMode([{ value: "classic", checked: false }]), null);
  assert.strictEqual(arch.chosenMode([]), null);
  assert.strictEqual(arch.chosenMode(undefined), null);
});
test("the one sentence says what is sent and that switching back is instant", () => {
  assert.ok(/Sarvam/.test(arch.sentence));
  assert.ok(/instant/.test(arch.sentence));
  assert.ok(/conversation/.test(arch.sentence));
});

if (failures.length) {
  console.log(failures.join("\n"));
  console.log(passed + " passed, " + failures.length + " FAILED");
  process.exit(1);
}
console.log(passed + " passed");
