// Node tests for the pure helpers of static/help.js (the "Need help" tab) and static/help_notice.js.
// No DOM, no network. Run: node tests/help.test.js (tests/test_help_ui_wiring.py runs it in the suite.)
global.window = {};
const assert = require("assert");
const h = require("../static/help.js");
const notice = require("../static/help_notice.js");

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed += 1; } catch (e) { failures.push(name + ": " + (e && e.message)); }
}
const MB = 1024 * 1024;
const file = (name, size) => ({ name: name, size: size });
const limits = h.DEFAULT_LIMITS;

test("an extension is the last part after the dot, lower-cased, ignoring any folder", () => {
  assert.strictEqual(h.fileExtension("Shot.PNG"), "png");
  assert.strictEqual(h.fileExtension("a.b.pdf"), "pdf");
  assert.strictEqual(h.fileExtension("C:\\x.y\\readme"), "");
  assert.strictEqual(h.fileExtension("noext"), "");
  assert.strictEqual(h.fileExtension(null), "");
});

test("sizes read as B, KB or MB", () => {
  assert.strictEqual(h.formatBytes(0), "0 B");
  assert.strictEqual(h.formatBytes(1023), "1023 B");
  assert.strictEqual(h.formatBytes(2048), "2.0 KB");
  assert.strictEqual(h.formatBytes(20480), "20 KB");
  assert.strictEqual(h.formatBytes(10 * MB), "10.0 MB");
});

test("allowed files are accepted and each allowed type works", () => {
  const first = h.validateFiles([], ["a.png", "a.jpg", "a.jpeg", "a.webp", "a.gif"].map((n) => file(n, 10)), limits);
  assert.strictEqual(first.accepted.length, 5);
  assert.deepStrictEqual(first.rejected, []);
  const types = h.validateFiles([], ["a.heic", "a.docx", "a.xlsx", "a.csv", "A.PNG"].map((n) => file(n, 5)), limits);
  assert.strictEqual(types.accepted.length, 5);
  const more = h.validateFiles([], [file("a.pdf", 5), file("a.txt", 5)], limits);
  assert.strictEqual(more.accepted.length, 2);
});

test("videos, svg, html, scripts, archives, executables and macro files are refused", () => {
  const bad = ["clip.mp4", "clip.mov", "x.svg", "x.html", "x.js", "x.exe", "x.zip", "x.docm", "x.xlsm", "x.sh", "x.png.exe", "noext", "x.heif"];
  const r = h.validateFiles([], bad.map((n) => file(n, 10)), limits);
  assert.strictEqual(r.accepted.length, 0);
  assert.strictEqual(r.rejected.length, bad.length);
  assert.ok(/cannot be attached/.test(r.rejected[0].reason));
});

test("empty and oversized files are refused; exactly 10 MB is fine", () => {
  assert.strictEqual(h.validateFiles([], [file("a.png", 0)], limits).rejected[0].reason, "the file is empty");
  assert.ok(/larger than 10.0 MB/.test(h.validateFiles([], [file("a.png", 10 * MB + 1)], limits).rejected[0].reason));
  assert.strictEqual(h.validateFiles([], [file("a.png", 10 * MB)], limits).accepted.length, 1);
});

test("at most 5 files, counting those already chosen", () => {
  const current = [1, 2, 3, 4].map((n) => file("f" + n + ".png", 10));
  const r = h.validateFiles(current, [file("g1.png", 10), file("g2.png", 10)], limits);
  assert.deepStrictEqual(r.accepted.map((f) => f.name), ["g1.png"]);
  assert.ok(/at most 5 files/.test(r.rejected[0].reason));
});

test("25 MB in all, counting those already chosen", () => {
  const current = [file("a.png", 10 * MB), file("b.png", 10 * MB)];
  const r = h.validateFiles(current, [file("c.png", 6 * MB), file("d.png", 5 * MB)], limits);
  assert.deepStrictEqual(r.accepted.map((f) => f.name), ["d.png"]);
  assert.ok(/together/.test(r.rejected[0].reason));
});

test("the same file twice is only added once", () => {
  const r = h.validateFiles([file("a.png", 10)], [file("a.png", 10), file("a.png", 11)], limits);
  assert.deepStrictEqual(r.accepted.map((f) => f.size), [11]);
  assert.strictEqual(r.rejected[0].reason, "it is already added");
});

test("the current list is not changed by validating", () => {
  const current = [file("a.png", 10)];
  h.validateFiles(current, [file("b.png", 10)], limits);
  assert.strictEqual(current.length, 1);
});

test("a stamp is shown as day, month, year and time", () => {
  assert.strictEqual(h.formatStamp("2026-10-11 12:15:32"), "11 Oct 2026, 12:15");
  assert.strictEqual(h.formatStamp("2026-01-05 09:05:00"), "5 Jan 2026, 09:05");
  assert.strictEqual(h.formatStamp(null), "-");
  assert.strictEqual(h.formatStamp("soon"), "soon");
});

test("durations read in the largest sensible unit", () => {
  assert.strictEqual(h.durationText(30), "less than a minute");
  assert.strictEqual(h.durationText(90), "1 min");
  assert.strictEqual(h.durationText(2 * 3600 + 5 * 60), "2 h 5 min");
  assert.strictEqual(h.durationText(26 * 3600), "1 d 2 h");
  assert.strictEqual(h.durationText(-3600), "1 h 0 min");
});

test("time left: counting down, then overdue, and nothing once resolved", () => {
  assert.strictEqual(h.timeLeftText(23 * 3600 + 10 * 60), "23 h 10 min left");
  assert.strictEqual(h.timeLeftText(-(2 * 3600 + 5 * 60)), "overdue by 2 h 5 min");
  assert.strictEqual(h.timeLeftText(10), "less than a minute left");
  assert.strictEqual(h.timeLeftText(null), "");
  assert.strictEqual(h.timeLeftText(undefined), "");
});

test("SLA labels and the one-line summary", () => {
  assert.strictEqual(h.slaLabel("on_track"), "On track");
  assert.strictEqual(h.slaLabel("due_soon"), "Due soon");
  assert.strictEqual(h.slaLabel("overdue"), "Overdue");
  assert.strictEqual(h.slaLabel("met"), "Met");
  assert.strictEqual(h.slaLabel("breached"), "Breached");
  assert.strictEqual(h.slaLine({ sla_state: "due_soon", seconds_left: 3 * 3600 }), "Due soon, 3 h 0 min left");
  assert.strictEqual(h.slaLine({ sla_state: "met", seconds_left: null }), "Met");
  assert.strictEqual(h.slaLine({ sla_state: "overdue", seconds_left: -600 }), "Overdue, overdue by 10 min");
});

test("the SLA as shown on the confirmation: hours and due time", () => {
  assert.strictEqual(h.slaDueText(24, "2026-10-06 10:00:00"), "24 hours, due 6 Oct 2026, 10:00");
  assert.strictEqual(h.slaDueText(1, "2026-10-06 10:00:00"), "1 hour, due 6 Oct 2026, 10:00");
});

test("status labels", () => {
  assert.strictEqual(h.statusLabel("in_progress"), "In progress");
  assert.strictEqual(h.statusLabel("new"), "New");
  assert.strictEqual(h.statusLabel("odd"), "odd");
});

test("the character counter", () => {
  assert.strictEqual(h.counterText(12, 4000), "12 / 4000");
});

test("sending needs words: 10 characters, or a file and a few words", () => {
  assert.strictEqual(h.canSubmit("", 0, limits).ok, false);
  assert.strictEqual(h.canSubmit("   ", 3, limits).ok, false);
  assert.strictEqual(h.canSubmit("too short", 0, limits).ok, false);
  assert.strictEqual(h.canSubmit("long enough!", 0, limits).ok, true);
  assert.strictEqual(h.canSubmit("slow", 1, limits).ok, true);
  assert.strictEqual(h.canSubmit("x".repeat(4001), 0, limits).ok, false);
  assert.strictEqual(h.canSubmit("[number hidden]", 1, limits).ok, false);
  assert.ok(/at least 10 characters/.test(h.canSubmit("short", 0, limits).reason));
});

test("source is typed, voice or mixed by what the person did", () => {
  assert.strictEqual(h.sourceFor({ typed: true, dictated: false }), "typed");
  assert.strictEqual(h.sourceFor({ typed: false, dictated: true }), "voice");
  assert.strictEqual(h.sourceFor({ typed: true, dictated: true }), "mixed");
  assert.strictEqual(h.sourceFor({}), "typed");
  assert.strictEqual(h.sourceFor(null), "typed");
});

test("a dictation error is a friendly line, never the speech service's own words", () => {
  const msg = h.dictationErrorText;
  assert.strictEqual(msg("Audio frame 25600 bytes exceeds the per-frame cap of 16000 bytes for stream_type 'fast'."), "Dictation stopped. You can keep typing.");
  assert.strictEqual(msg(""), "Dictation stopped. You can keep typing.");
  assert.strictEqual(msg(undefined), "Dictation stopped. You can keep typing.");
  assert.strictEqual(msg("Voice is off: add SARVAM_API_KEY to the .env file and restart the app."), "Voice is off: add SARVAM_API_KEY to the .env file and restart the app. You can keep typing.");
  assert.ok(msg("Microphone access failed: denied. Allow microphone access and try again.").startsWith("Microphone access failed"));
  assert.strictEqual(msg("Lost the connection. Dictation stopped."), "Lost the connection. Dictation stopped. You can keep typing.");
});

test("dictated text is added after what is there with one space, and cut at the limit", () => {
  assert.strictEqual(h.appendDictation("", "the screen is slow", 4000), "the screen is slow");
  assert.strictEqual(h.appendDictation("It is slow.", "  and confusing ", 4000), "It is slow. and confusing");
  assert.strictEqual(h.appendDictation("Line one\n", "line two", 4000), "Line one\nline two");
  assert.strictEqual(h.appendDictation("abc", "", 4000), "abc");
  assert.strictEqual(h.appendDictation("abc", "defgh", 6), "abc de");
});

test("a draft is stored as the three fields and read back; anything else is ignored", () => {
  const text = h.draftToText({ category: "other", severity: "annoying", description: "hello there" });
  assert.deepStrictEqual(h.draftFromText(text, 4000), { category: "other", severity: "annoying", description: "hello there" });
  assert.strictEqual(h.draftFromText(null, 4000), null);
  assert.strictEqual(h.draftFromText("not json", 4000), null);
  assert.strictEqual(h.draftFromText("[1,2]", 4000), null);
  assert.strictEqual(h.draftFromText('{"v":2,"description":"x"}', 4000), null);
  assert.strictEqual(h.draftFromText('{"v":1,"category":"","severity":"","description":""}', 4000), null);
  assert.strictEqual(h.draftFromText('{"v":1,"description":5}', 4000), null);
  assert.strictEqual(h.draftFromText(h.draftToText({ description: "x".repeat(50) }), 10).description.length, 10);
});

test("a draft never holds files or anything else", () => {
  const parsed = JSON.parse(h.draftToText({ category: "other", severity: "minor", description: "d", files: ["x"], username: "me" }));
  assert.deepStrictEqual(Object.keys(parsed).sort(), ["category", "description", "severity", "v"]);
});

test("history lines say who did what, with the note", () => {
  assert.strictEqual(h.historyText({ at: "2026-10-05 10:00:00", actor: "user", kind: "created", to_status: "new" }), "5 Oct 2026, 10:00 \u2014 You: Request sent");
  assert.strictEqual(h.historyText({ at: "2026-10-05 12:00:00", actor: "import", kind: "status", from_status: "new", to_status: "resolved", note: "Fixed" }),
    "5 Oct 2026, 12:00 \u2014 Update from the developer: Status changed from New to Resolved. Fixed");
  assert.ok(/Team: Note added/.test(h.historyText({ at: "2026-10-05 12:00:00", actor: "team", kind: "note", note: "n" })));
});

test("the team summary line", () => {
  assert.strictEqual(h.summaryText({ total: 0 }), "No requests yet.");
  const text = h.summaryText({ total: 4, by_status: { "new": 1, acknowledged: 0, in_progress: 2, resolved: 1, closed: 0 },
                               by_sla: { on_track: 1, due_soon: 0, overdue: 2, met: 0, breached: 1 } });
  assert.strictEqual(text, "4 requests: 1 new, 2 in progress, 1 resolved. SLA: 2 overdue, 1 breached.");
  assert.strictEqual(h.summaryText({ total: 1, by_status: { "new": 1 }, by_sla: { on_track: 1 } }), "1 request: 1 new.");
});

test("the import result is explained in plain lines", () => {
  const lines = h.importSummaryLines({ rows: 5, updated: 2, notes_added: 1, unchanged: 1, stale: 1, unknown_tickets: ["HELP-0099"],
                                       invalid: [{ row: 4, ticket_no: "HELP-0001", reason: "unknown status" }] });
  assert.strictEqual(lines[0], "Read 5 rows: 2 updated, 1 notes added, 1 already up to date.");
  assert.ok(/1 skipped because the request had already moved on/.test(lines[1]));
  assert.strictEqual(lines[2], "Not found in this app: HELP-0099.");
  assert.strictEqual(lines[3], "Row 4 (HELP-0001) not used: unknown status.");
  assert.strictEqual(h.importSummaryLines({ rows: 1, updated: 1, notes_added: 0, unchanged: 0, stale: 0, unknown_tickets: [], invalid: [] }).length, 1);
});

test("the resolved notice uses the server's sentence, or builds the same one", () => {
  assert.strictEqual(notice.noticeText({ text: "Your request HELP-0001 has been resolved. Done." }), "Your request HELP-0001 has been resolved. Done.");
  assert.strictEqual(notice.noticeText({ ticket_no: "HELP-0002", note: "Fixed in the update. " }), "Your request HELP-0002 has been resolved. Fixed in the update.");
  assert.strictEqual(notice.noticeText({ ticket_no: "HELP-0003", note: null }), "Your request HELP-0003 has been resolved.");
});

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(passed + " passed");
