// Node tests for the hold-to-talk client logic in static/live_voice.js: the
// pure key-decision function and the held-key state machine. No DOM, no mic,
// no socket -- the browser globals are stubbed exactly like the other
// no-browser script tests. Run: node tests/ptt_client.test.js
// (tests/test_ptt_client.py runs this inside the normal unittest suite.)
global.document = { addEventListener() {} };
global.window = {};
const exported = require("../static/live_voice.js");

const assert = require("assert");
const { pttShouldStart, pttIsTalkKey, pttElementInfo, createHoldMachine } = window;

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

const body = { tag: "body" };
const key = (k, extra) => Object.assign({ key: k, repeat: false, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false }, extra || {});
const START = { start: true, preventDefault: true };
const IGNORE = { start: false, preventDefault: false };

// ---- which keys are talk keys ----------------------------------------------
test("talk keys are Enter and F1 only", () => {
  ["Enter", "F1"].forEach((k) => assert.ok(pttIsTalkKey(k), k));
  ["F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "F13", "F0", "F", "f1", " ", "Escape", "Tab", "a", "Shift", "Fn", "Control", "Meta", "ArrowUp", "NumpadEnter", ""].forEach((k) => assert.ok(!pttIsTalkKey(k), k));
});

test("F1 starts a listen on the body; no other F-key does", () => {
  assert.deepStrictEqual(pttShouldStart(key("F1"), body), START);
  for (let i = 2; i <= 12; i++) assert.deepStrictEqual(pttShouldStart(key("F" + i), body), IGNORE, "F" + i);
});

test("a non-talk key is ignored and not prevented", () => {
  ["a", " ", "Tab", "Escape", "F2", "F5", "F12", "ArrowDown"].forEach((k) => assert.deepStrictEqual(pttShouldStart(key(k), body), IGNORE, k));
});

// ---- modifiers --------------------------------------------------------------
test("Cmd/Ctrl/Alt/Meta held: browser shortcut, never ours (Enter and F-keys)", () => {
  ["ctrlKey", "metaKey", "altKey"].forEach((m) => {
    ["Enter", "F1"].forEach((k) => {
      const ev = key(k); ev[m] = true;
      assert.deepStrictEqual(pttShouldStart(ev, body), IGNORE, m + "+" + k);
    });
  });
});

test("Shift alone does not block a talk key", () => {
  assert.deepStrictEqual(pttShouldStart(key("F1", { shiftKey: true }), body), START);
  assert.deepStrictEqual(pttShouldStart(key("Enter", { shiftKey: true }), body), START);
});

test("IME composition is never a talk key", () => {
  assert.deepStrictEqual(pttShouldStart(key("Enter", { isComposing: true }), body), IGNORE);
});

// ---- repeat -----------------------------------------------------------------
test("auto-repeat never starts a listen but is still claimed (no repeated clicks / reloads)", () => {
  assert.deepStrictEqual(pttShouldStart(key("F1", { repeat: true }), body), { start: false, preventDefault: true });
  assert.deepStrictEqual(pttShouldStart(key("Enter", { repeat: true }), body), { start: false, preventDefault: true });
});

test("repeat of Enter in a text field is not claimed", () => {
  assert.deepStrictEqual(pttShouldStart(key("Enter", { repeat: true }), { tag: "input" }), IGNORE);
});

// ---- Enter and focus --------------------------------------------------------
test("Enter in input/textarea/select/contenteditable is left alone", () => {
  [{ tag: "input" }, { tag: "input", type: "text" }, { tag: "textarea" }, { tag: "select" },
    { tag: "div", contentEditable: true }, { tag: "p", contentEditable: true }].forEach((info) => {
    assert.deepStrictEqual(pttShouldStart(key("Enter"), info), IGNORE, JSON.stringify(info));
  });
});

test("Enter on a button, link or role=button still activates it (not a talk key)", () => {
  [{ tag: "button" }, { tag: "a" }, { tag: "div", role: "button" }, { tag: "span", role: "BUTTON" },
    { tag: "summary" }, { tag: "div", role: "link" }, { tag: "div", role: "textbox" }].forEach((info) => {
    assert.deepStrictEqual(pttShouldStart(key("Enter"), info), IGNORE, JSON.stringify(info));
  });
});

test("Enter on a left-nav item starts a listen (and is claimed so it does not re-click)", () => {
  assert.deepStrictEqual(pttShouldStart(key("Enter"), { tag: "button", isNavItem: true }), START);
  assert.deepStrictEqual(pttShouldStart(key("Enter"), { tag: "button", role: "button", isNavItem: true }), START);
});

test("Enter with body / nothing / a plain container focused starts a listen", () => {
  assert.deepStrictEqual(pttShouldStart(key("Enter"), body), START);
  assert.deepStrictEqual(pttShouldStart(key("Enter"), { tag: "div" }), START);
  assert.deepStrictEqual(pttShouldStart(key("Enter"), { tag: "html" }), START);
  assert.deepStrictEqual(pttShouldStart(key("Enter"), null), START);
  assert.deepStrictEqual(pttShouldStart(key("Enter"), undefined), START);
});

test("F1 works (and is claimed) wherever focus is", () => {
  [{ tag: "input" }, { tag: "textarea" }, { tag: "select" }, { tag: "button" }, { tag: "a" },
    { tag: "div", contentEditable: true }, { tag: "div", role: "button" }].forEach((info) => {
    assert.deepStrictEqual(pttShouldStart(key("F1"), info), START, JSON.stringify(info));
  });
});

test("pttElementInfo snapshots an element (and tolerates null)", () => {
  assert.strictEqual(pttElementInfo(null).tag, "body");
  const nav = { tagName: "BUTTON", isContentEditable: false, getAttribute: () => null, closest: (sel) => (sel === ".nav-link" ? {} : null) };
  assert.deepStrictEqual(pttElementInfo(nav), { tag: "button", role: "", contentEditable: false, isNavItem: true });
  const editor = { tagName: "DIV", isContentEditable: true, getAttribute: (a) => (a === "role" ? "textbox" : null), closest: () => null };
  assert.deepStrictEqual(pttElementInfo(editor), { tag: "div", role: "textbox", contentEditable: true, isNavItem: false });
});

// ---- the held-key state machine ---------------------------------------------
function harness(opts) {
  const log = [];
  let t = 1000;
  const timers = [];
  const m = createHoldMachine({
    onStart: (id) => { log.push(["start", id]); return opts && opts.refuse ? false : true; },
    onStop: (reason, held, id) => log.push(["stop", reason, held, id]),
    onTooShort: (held, id) => log.push(["short", held, id]),
    onCancel: (reason, id) => log.push(["cancel", reason, id]),
  }, {
    now: () => t,
    setTimeout: (fn, ms) => { const h = { fn, ms, cleared: false }; timers.push(h); return h; },
    clearTimeout: (h) => { if (h) h.cleared = true; },
  });
  return { m, log, advance: (ms) => { t += ms; }, timers };
}

test("press then release after a real hold -> start, stop", () => {
  const { m, log, advance } = harness();
  assert.strictEqual(m.press("Enter"), true);
  assert.ok(m.isHeld());
  advance(1200);
  assert.strictEqual(m.release("Enter"), true);
  assert.ok(!m.isHeld());
  assert.deepStrictEqual(log, [["start", "Enter"], ["stop", "release", 1200, "Enter"]]);
});

test("a tap shorter than the minimum is cancelled, not sent", () => {
  const { m, log, advance } = harness();
  m.press("F1");
  advance(120);
  m.release("F1");
  assert.deepStrictEqual(log, [["start", "F1"], ["short", 120, "F1"]]);
});

test("the minimum is ~300 ms: 299 is a tap, 300 is a listen", () => {
  let h = harness(); h.m.press("F1"); h.advance(299); h.m.release("F1");
  assert.strictEqual(h.log[1][0], "short");
  h = harness(); h.m.press("F1"); h.advance(300); h.m.release("F1");
  assert.strictEqual(h.log[1][0], "stop");
});

test("only one listen at a time: a second press while held is ignored", () => {
  const { m, log, advance } = harness();
  assert.strictEqual(m.press("Enter"), true);
  assert.strictEqual(m.press("F1"), false);
  assert.strictEqual(m.press("pointer"), false);
  assert.strictEqual(m.heldId(), "Enter");
  advance(500);
  m.release("Enter");
  assert.deepStrictEqual(log.map((e) => e[0]), ["start", "stop"]);
});

test("a different key's keyup is ignored; only the held key ends it", () => {
  const { m, log, advance } = harness();
  m.press("Enter");
  advance(500);
  assert.strictEqual(m.release("F1"), false);
  assert.strictEqual(m.release("pointer"), false);
  assert.ok(m.isHeld());
  assert.strictEqual(m.release("Enter"), true);
  assert.deepStrictEqual(log.map((e) => e[0]), ["start", "stop"]);
});

test("a keyup with nothing held does nothing", () => {
  const { m, log } = harness();
  assert.strictEqual(m.release("Enter"), false);
  assert.strictEqual(m.end("blur"), false);
  assert.strictEqual(m.cancel("escape"), false);
  assert.deepStrictEqual(log, []);
});

test("after a release a new press works (a listen right after a listen)", () => {
  const { m, log, advance } = harness();
  m.press("Enter"); advance(400); m.release("Enter");
  assert.strictEqual(m.press("F1"), true);
  advance(400); m.release("F1");
  assert.deepStrictEqual(log.map((e) => e[0] + ":" + (e[3] || e[1])), ["start:Enter", "stop:Enter", "start:F1", "stop:F1"]);
});

test("Escape cancels (discard), and a later keyup is then ignored", () => {
  const { m, log, advance } = harness();
  m.press("Enter"); advance(900);
  assert.strictEqual(m.cancel("escape"), true);
  assert.ok(!m.isHeld());
  assert.strictEqual(m.release("Enter"), false);
  assert.deepStrictEqual(log, [["start", "Enter"], ["cancel", "escape", "Enter"]]);
});

test("blur / hidden / pagehide end the hold like a release", () => {
  ["blur", "hidden", "pagehide"].forEach((reason) => {
    const { m, log, advance } = harness();
    m.press("Enter"); advance(800);
    assert.strictEqual(m.end(reason), true);
    assert.deepStrictEqual(log[1], ["stop", reason, 800, "Enter"]);
    assert.ok(!m.isHeld());
  });
});

test("auto-release at 30 s: the timer is armed on press, and firing it stops the listen", () => {
  const { m, log, advance, timers } = harness();
  m.press("Enter");
  assert.strictEqual(timers.length, 1);
  assert.strictEqual(timers[0].ms, 30000);
  advance(30000);
  timers[0].fn();
  assert.ok(!m.isHeld());
  assert.deepStrictEqual(log[1], ["stop", "max", 30000, "Enter"]);
});

test("the auto-release timer is cleared when the key is released, cancelled or tapped", () => {
  let h = harness(); h.m.press("Enter"); h.advance(500); h.m.release("Enter");
  assert.ok(h.timers[0].cleared);
  h = harness(); h.m.press("Enter"); h.m.cancel("escape");
  assert.ok(h.timers[0].cleared);
  h = harness(); h.m.press("Enter"); h.advance(10); h.m.release("Enter");
  assert.ok(h.timers[0].cleared);
});

test("if onStart refuses (e.g. socket down) nothing is held and no timer is left running", () => {
  const { m, log, timers } = harness({ refuse: true });
  assert.strictEqual(m.press("Enter"), false);
  assert.ok(!m.isHeld());
  assert.strictEqual(timers.filter((t) => !t.cleared).length, 0);
  assert.deepStrictEqual(log, [["start", "Enter"]]);
  assert.strictEqual(m.release("Enter"), false);
});

test("the default limits are 300 ms and 30 s", () => {
  const { m, advance, log } = (() => { const l = []; let t = 0; const mm = createHoldMachine({ onStop: (...a) => l.push(["stop", ...a]), onTooShort: () => l.push(["short"]) }, { now: () => t, setTimeout: () => 1, clearTimeout: () => {} }); return { m: mm, log: l, advance: (ms) => { t += ms; } }; })();
  m.press("a"); advance(299); m.release("a");
  m.press("a"); advance(300); m.release("a");
  assert.deepStrictEqual(log.map((e) => e[0]), ["short", "stop"]);
});

// ---- exports ----------------------------------------------------------------
test("module.exports exposes the same functions", () => {
  assert.strictEqual(exported.pttShouldStart, pttShouldStart);
  assert.strictEqual(exported.createHoldMachine, createHoldMachine);
});

test("readAnswerSentence still behaves (patient lookup hides the sentence, Source footer stripped)", () => {
  const { readAnswerSentence } = window;
  assert.strictEqual(readAnswerSentence({ intent: "patient_lookup", data: { name: "x" }, answer_text: "Priya. Source: db" }), "");
  assert.strictEqual(readAnswerSentence({ intent: "queue_status", data: null, answer_text: "3 waiting. Source: queue, today" }), "3 waiting.");
});

test("list answers hide the redundant sentence when rows are shown, keep it when there are none", () => {
  const { readAnswerSentence } = window;
  const rows = [{ id: 1, patient_name: "Amit" }];
  assert.strictEqual(readAnswerSentence({ intent: "list_appointments", data: rows, answer_text: "1 appointment(s) on Mon 5 Oct. Source: x" }), "");
  assert.strictEqual(readAnswerSentence({ intent: "missed_followups", data: rows, answer_text: "1 follow-up(s) pending. Source: x" }), "");
  assert.strictEqual(readAnswerSentence({ intent: "list_appointments", data: [], answer_text: "No appointments on Wed 7 Oct. Source: x" }), "No appointments on Wed 7 Oct.");
  assert.strictEqual(readAnswerSentence({ intent: "queue_status", data: rows, answer_text: "3 waiting. Source: y" }), "3 waiting.");
});

test("Notes is always the last table column", () => {
  const { orderColumns } = window;
  assert.deepStrictEqual(orderColumns(["id", "appt_date", "notes", "patient_name", "patient_phone"]),
    ["id", "appt_date", "patient_name", "patient_phone", "notes"]);
  assert.deepStrictEqual(orderColumns(["id", "name"]), ["id", "name"]);
  assert.deepStrictEqual(orderColumns(["id", "start_time", "duration_minutes", "notes"]), ["id", "start_time", "notes"]);
  assert.deepStrictEqual(orderColumns(["Notes", "id"]), ["id", "Notes"]);
});

if (failures.length) {
  console.error(failures.length + " failed, " + passed + " passed");
  failures.forEach((f) => console.error("  FAIL " + f));
  process.exit(1);
}
console.log("ok " + passed + " passed");
