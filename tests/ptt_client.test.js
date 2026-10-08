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

test("branch and doctor columns show only with several branches; their ids and code never do", () => {
  const { orderColumns } = window;
  const keys = ["id", "appt_date", "branch_id", "doctor_id", "branch", "branch_code", "doctor", "notes"];
  assert.deepStrictEqual(orderColumns(keys), ["id", "appt_date", "notes"]);
  assert.deepStrictEqual(orderColumns(keys, false), ["id", "appt_date", "notes"]);
  assert.deepStrictEqual(orderColumns(keys, true), ["id", "appt_date", "branch", "doctor", "notes"]);
});

// ---- patients_list.js (pure helpers) ----------------------------------------
require("../static/patients_list.js");
test("patients count text says how many of how many are shown", () => {
  assert.strictEqual(window.patientsCountText(10, 27), "(showing 10 of 27)");
  assert.strictEqual(window.patientsCountText(27, 27), "(showing 27 of 27)");
});
test("patients page rows already shown are dropped, order kept", () => {
  const rows = [{ id: 7 }, { id: 6 }, { id: 5 }, { id: 5 }];
  assert.deepStrictEqual(window.patientsNewRows(["8", "7"], rows).map((r) => r.id), [6, 5]);
  assert.deepStrictEqual(window.patientsNewRows([], null), []);
  assert.deepStrictEqual(window.patientsNewRows([1, 2], [{ id: "2" }, { id: "3" }]).map((r) => r.id), ["3"]);
});

// ---- save_tick.js (the bare check mark beside a Save button) ----------------
(function () {
  function node(tag) {
    const n = {
      tag, className: "", textContent: "", children: [], parentNode: null, attrs: {}, listeners: {},
      setAttribute(k, v) { this.attrs[k] = v; },
      get nextSibling() { const i = this.parentNode.children.indexOf(this); return this.parentNode.children[i + 1] || null; },
      insertBefore(child, ref) { child.parentNode = this; const i = ref ? this.children.indexOf(ref) : this.children.length; this.children.splice(i, 0, child); },
      appendChild(child) { this.insertBefore(child, null); },
      remove() { if (this.parentNode) { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; } },
      querySelectorAll(sel) {
        const out = []; const walk = (x) => x.children.forEach((c) => { if (sel === ".save-tick" && c.className === "save-tick") out.push(c); walk(c); });
        walk(this); return out;
      },
      closest() { return null; },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); },
      fire(type) { (this.listeners[type] || []).slice().forEach((fn) => fn()); },
    };
    return n;
  }
  global.document.createElement = node;
  const timers = [];
  const realSetTimeout = global.setTimeout;
  global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  global.clearTimeout = () => {};
  require("../static/save_tick.js");
  global.setTimeout = realSetTimeout;

  function setup() {
    const row = node("div"); const save = node("button"); const after = node("span");
    row.appendChild(save); row.appendChild(after);
    return { row, save, after };
  }
  test("a save puts one bare check mark right after the button", () => {
    const { row, save, after } = setup();
    window.SaveTick.show(save);
    const tick = row.children[1];
    assert.strictEqual(tick.className, "save-tick");
    assert.strictEqual(tick.textContent, "\u2713");
    assert.strictEqual(tick.attrs["aria-label"], "Saved");
    assert.strictEqual(row.children[2], after);
  });
  test("saving again replaces the check mark instead of stacking another", () => {
    const { row, save } = setup();
    window.SaveTick.show(save); window.SaveTick.show(save);
    assert.strictEqual(row.querySelectorAll(".save-tick").length, 1);
  });
  test("changing something in the card takes the check mark away", () => {
    const { row, save } = setup();
    window.SaveTick.show(save);
    row.fire("input");
    assert.strictEqual(row.querySelectorAll(".save-tick").length, 0);
  });
  test("a button with no parent gets nothing, and clear removes it", () => {
    assert.strictEqual(window.SaveTick.show(node("button")), null);
    const { row, save } = setup();
    window.SaveTick.show(save); window.SaveTick.clear(row);
    assert.strictEqual(row.querySelectorAll(".save-tick").length, 0);
  });
})();

// ---- branches.js (pure helpers) ---------------------------------------------
require("../static/branches.js");
const BR = [{ id: 1, name: "Branch A" }, { id: 2, name: "Branch B" }, { id: 3, name: "Branch C" }];
test("pickBranchId keeps a valid stored branch and falls back otherwise", () => {
  assert.strictEqual(window.pickBranchId("2", BR, 1), 2);
  assert.strictEqual(window.pickBranchId(3, BR, 1), 3);
  assert.strictEqual(window.pickBranchId("9", BR, 1), 1);
  assert.strictEqual(window.pickBranchId(null, BR, 1), 1);
  assert.strictEqual(window.pickBranchId("all", BR, 1), 1);
});
test("pickViewBranch allows 'all' only when there is more than one branch", () => {
  assert.strictEqual(window.pickViewBranch("all", BR, 1), "all");
  assert.strictEqual(window.pickViewBranch("all", [BR[0]], 1), 1);
  assert.strictEqual(window.pickViewBranch("3", BR, 1), 3);
  assert.strictEqual(window.pickViewBranch("junk", BR, 2), 2);
});
test("Branches falls back to defaults when the page carries no data", () => {
  assert.strictEqual(window.Branches.multi(), false);
  assert.strictEqual(window.Branches.mine(), 1);
  assert.strictEqual(window.Branches.view(), 1);
  assert.strictEqual(window.Branches.viewQuery(), "branch=1");
});

// ---- calendar_view.js (pure date helpers) -----------------------------------
require("../static/calendar_view.js");
test("week starts on the Monday on or before the date", () => {
  assert.strictEqual(window.cvWeekStart("2026-10-05"), "2026-10-05");   // a Monday
  assert.strictEqual(window.cvWeekStart("2026-10-11"), "2026-10-05");   // the Sunday after
  assert.strictEqual(window.cvWeekStart("2026-10-07"), "2026-10-05");
  assert.deepStrictEqual(window.cvRange("week", "2026-10-07"), { start: "2026-10-05", end: "2026-10-11" });
});
test("month range covers whole weeks, across year ends too", () => {
  assert.deepStrictEqual(window.cvMonthRange("2026-10-15"), { start: "2026-09-28", end: "2026-11-01" });
  assert.deepStrictEqual(window.cvMonthRange("2026-12-10"), { start: "2026-11-30", end: "2027-01-03" });
});
test("agenda is two weeks from the anchor", () => {
  assert.deepStrictEqual(window.cvRange("agenda", "2026-10-05"), { start: "2026-10-05", end: "2026-10-18" });
});
test("previous / next move by the right amount", () => {
  assert.strictEqual(window.cvShift("week", "2026-10-05", 1), "2026-10-12");
  assert.strictEqual(window.cvShift("week", "2026-10-05", -1), "2026-09-28");
  assert.strictEqual(window.cvShift("agenda", "2026-10-05", 1), "2026-10-19");
  assert.strictEqual(window.cvShift("month", "2026-10-31", 1), "2026-11-01");
  assert.strictEqual(window.cvShift("month", "2026-01-15", -1), "2025-12-01");
});
test("date arithmetic never slips a day across month ends", () => {
  assert.strictEqual(window.cvAddDays("2026-02-28", 1), "2026-03-01");
  assert.strictEqual(window.cvAddDays("2028-02-28", 1), "2028-02-29");
  assert.strictEqual(window.cvAddDays("2026-12-31", 1), "2027-01-01");
});
test("titles and day labels", () => {
  assert.strictEqual(window.cvTitle("month", "2026-10-05"), "October 2026");
  assert.strictEqual(window.cvTitle("week", "2026-10-07"), "5 Oct - 11 Oct 2026");
  assert.strictEqual(window.cvDayLabel("2026-10-07"), "Wed 7 Oct");
});
test("the week grid widens only when an appointment falls outside 08:00-20:00", () => {
  assert.deepStrictEqual(window.cvHourBounds([{ start_time: "09:00", end_time: "09:30" }]), { start: 480, end: 1200 });
  assert.deepStrictEqual(window.cvHourBounds([{ start_time: "07:30", end_time: "08:00" }, { start_time: "20:30", end_time: "21:15" }]), { start: 420, end: 1320 });
});
test("appointments are grouped by day", () => {
  const g = window.cvGroupByDay([{ appt_date: "2026-10-05", id: 1 }, { appt_date: "2026-10-06", id: 2 }, { appt_date: "2026-10-05", id: 3 }]);
  assert.deepStrictEqual(g["2026-10-05"].map((a) => a.id), [1, 3]);
});

// ---- closures.js (pure helpers) -----------------------------------------------
require("../static/closures.js");
test("a closure option round-trips through its select value", () => {
  const option = { branch_id: 2, date: "2026-10-06", time: "10:30" };
  assert.strictEqual(window.closureOptionValue(option), "2|2026-10-06|10:30");
  assert.deepStrictEqual(window.closureParseValue("2|2026-10-06|10:30"), { to_branch_id: 2, to_date: "2026-10-06", to_time: "10:30" });
  assert.strictEqual(window.closureOptionValue(null), "");
  for (const bad of ["", "2|2026-10-06", "||", "a|b", null, undefined]) assert.strictEqual(window.closureParseValue(bad), null);
});

test("a closure row sends a target only for a move", () => {
  assert.deepStrictEqual(window.closureRowPayload(7, "move", "3|2026-10-06|09:00"),
    { appointment_id: 7, action: "move", to_branch_id: 3, to_date: "2026-10-06", to_time: "09:00" });
  assert.deepStrictEqual(window.closureRowPayload(7, "cancel", "3|2026-10-06|09:00"), { appointment_id: 7, action: "cancel" });
  assert.deepStrictEqual(window.closureRowPayload(7, "leave", ""), { appointment_id: 7, action: "leave" });
  // a move with nothing chosen carries no target, so the card can tell and ask
  assert.strictEqual(window.closureRowPayload(7, "move", "").to_branch_id, undefined);
});

test("the closure summary says what happened and what the patients were told", () => {
  const counts = { moved: 5, cancelled: 1, failed: 1, left: 2 };
  assert.strictEqual(window.closureSummaryLine(counts, null), "5 moved, 1 cancelled, 2 left as they were, 1 could not be changed.");
  assert.strictEqual(window.closureSummaryLine({ moved: 2, cancelled: 0, failed: 0, left: 0 }, { sent: 1, waiting: 1, recorded: 0, failed: 0 }),
    "2 moved. 1 told on WhatsApp; 1 waiting to be sent (outside WhatsApp's 24-hour window).");
  assert.strictEqual(window.closureSummaryLine({ moved: 0, cancelled: 0, failed: 0, left: 0 }, null), "No appointments were touched.");
});

// ---- patients_subtabs.js (pure helpers) ---------------------------------------
require("../static/patients_subtabs.js");
const SUBTABS = ["patients", "missed", "attendance", "followups"];
test("a remembered sub-tab is used only if it exists", () => {
  assert.strictEqual(window.pickSubtab("followups", SUBTABS, "patients"), "followups");
  assert.strictEqual(window.pickSubtab("missed", SUBTABS, "patients"), "missed");
  for (const bad of [null, undefined, "", "queue", "Patients", "followups ", 3]) assert.strictEqual(window.pickSubtab(bad, SUBTABS, "patients"), "patients");
});
test("arrow keys move between sub-tabs and wrap round", () => {
  assert.strictEqual(window.subtabNeighbour(SUBTABS, "patients", "ArrowRight"), "missed");
  assert.strictEqual(window.subtabNeighbour(SUBTABS, "followups", "ArrowRight"), "patients");
  assert.strictEqual(window.subtabNeighbour(SUBTABS, "patients", "ArrowLeft"), "followups");
  assert.strictEqual(window.subtabNeighbour(SUBTABS, "missed", "ArrowLeft"), "patients");
  assert.strictEqual(window.subtabNeighbour(SUBTABS, "missed", "Enter"), null);
  assert.strictEqual(window.subtabNeighbour(SUBTABS, "nope", "ArrowRight"), null);
});

// ---- followups.js (pure helpers) ------------------------------------------------
require("../static/followups.js");
test("a follow-up day reads like 'Wed 7 Oct' and never slips across a month end", () => {
  assert.strictEqual(window.fuDayLabel("2026-10-07"), "Wed 7 Oct");
  assert.strictEqual(window.fuDayLabel("2026-12-31"), "Thu 31 Dec");
  assert.strictEqual(window.fuDayLabel("2028-02-29"), "Tue 29 Feb");
  assert.strictEqual(window.fuVisitLabel("2026-10-07", "10:30"), "Wed 7 Oct 10:30");
  assert.strictEqual(window.fuVisitLabel("2026-10-07", ""), "Wed 7 Oct");
  assert.strictEqual(window.fuDayLabel("soon"), "soon");
});
test("the patient box maps back to a patient id only on an exact match", () => {
  const options = [{ value: "Sunita Devi · 9876543210", id: "4" }, { value: "Sunita Devi · 9876543299", id: "9" }];
  assert.strictEqual(window.fuPatientId("Sunita Devi · 9876543299", options), 9);
  assert.strictEqual(window.fuPatientId("  Sunita Devi · 9876543210 ", options), 4);
  for (const bad of ["Sunita", "", null, undefined, "sunita devi · 9876543210"]) assert.strictEqual(window.fuPatientId(bad, options), null);
});
test("a form row becomes the payload the server expects", () => {
  assert.deepStrictEqual(window.fuRowPayload({ patientId: 4, date: "2026-10-07", time: "10:30", doctor: "1", branch: "2", diagnosis: "  note  " }),
    { patient_id: 4, due_date: "2026-10-07", due_time: "10:30", doctor_id: 1, branch_id: 2, diagnosis: "note" });
  assert.deepStrictEqual(window.fuRowPayload({ patientId: null, date: "", time: "", doctor: "", branch: "", diagnosis: "   " }),
    { patient_id: null, due_date: "", due_time: "", doctor_id: null, branch_id: null, diagnosis: null });
});
test("each reminder state has its own badge", () => {
  assert.deepStrictEqual(window.fuReminderBadge({ status: "queued" }), { cls: "fu-badge fu-badge-queued", text: "Queued" });
  assert.strictEqual(window.fuReminderBadge({ status: "blocked" }).text, "Blocked (outside 24h window)");
  for (const s of ["sent", "failed", "skipped"]) assert.strictEqual(window.fuReminderBadge({ status: s }).cls, "fu-badge fu-badge-" + s);
});
test("the apply summary counts booked and failed rows", () => {
  assert.strictEqual(window.fuApplySummary({ created: 1, failed: 0 }), "1 follow-up booked.");
  assert.strictEqual(window.fuApplySummary({ created: 3, failed: 2 }), "3 follow-ups booked, 2 could not be booked.");
  assert.strictEqual(window.fuApplySummary({ created: 0, failed: 1 }), "0 follow-ups booked, 1 could not be booked.");
});
test("reminder timing is checked the way the server checks it", () => {
  const good = { days_before: "2", send_time: "10:00", hours_before: 4, earliest_send: "07:00" };
  assert.deepStrictEqual(window.fuTimingProblems(good), []);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { days_before: "0" })).length, 1);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { days_before: "15" })).length, 1);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { days_before: "2.5" })).length, 1);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { send_time: "04:59" })).length, 1);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { send_time: "23:00" })).length, 1);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { hours_before: 13 })).length, 1);
  assert.strictEqual(window.fuTimingProblems(Object.assign({}, good, { earliest_send: "" })).length, 1);
  assert.strictEqual(window.fuTimingProblems({}).length, 4);
  assert.deepStrictEqual(window.fuTimingProblems({ days_before: 14, send_time: "22:59", hours_before: 12, earliest_send: "05:00" }), []);
});

// ---- the planner's "thinking" state ------------------------------------------
test("the thinking line says what is happening, and a bad event says nothing", () => {
  const { thinkingNote } = window;
  assert.strictEqual(thinkingNote({ transcript: "x", stage: "planner" }), "Working out what you meant…");
  assert.strictEqual(thinkingNote({ transcript: "x" }), "Thinking…");
  for (const bad of [null, undefined, "planner", 7]) assert.strictEqual(thinkingNote(bad), "");
  assert.strictEqual(exported.thinkingNote, thinkingNote);
});

test("the page waits longer than the planner's 12 s limit plus the fallback before giving up", () => {
  assert.ok(window.THINKING_SAFETY_MS >= 12000 + 15000, window.THINKING_SAFETY_MS);
});

if (failures.length) {
  console.error(failures.length + " failed, " + passed + " passed");
  failures.forEach((f) => console.error("  FAIL " + f));
  process.exit(1);
}
console.log("ok " + passed + " passed");
