// Closures: close a branch (or one doctor) for some days and move its patients in
// ONE reviewed batch.
//
//   ClosureCard.build(plan, opts)  the batch review card. One row per booked
//       patient: where they are now, and what to do with them (move to a
//       suggested branch/day/time, cancel, or leave as is). Nothing happens until
//       Apply. Used by the Automation tab and by the voice assistant ("close
//       Branch A tomorrow, the doctor is ill").
//   The Automation tab section: the form, the history of closures, and Undo.
//
// Every value comes from the server as data and is inserted with textContent.
// The pure helpers at the top are exported for tests/ptt_client.test.js.

window.closureOptionValue = function (option) {
  return option ? [option.branch_id, option.date, option.time].join("|") : "";
};

window.closureParseValue = function (value) {
  var parts = String(value || "").split("|");
  if (parts.length !== 3 || !parts[0] || !parts[1] || !parts[2]) return null;
  return { to_branch_id: parseInt(parts[0], 10), to_date: parts[1], to_time: parts[2] };
};

// What one row sends to /closures/apply.
window.closureRowPayload = function (appointmentId, action, optionValue) {
  var row = { appointment_id: appointmentId, action: action };
  if (action === "move") {
    var target = window.closureParseValue(optionValue);
    if (target) { row.to_branch_id = target.to_branch_id; row.to_date = target.to_date; row.to_time = target.to_time; }
  }
  return row;
};

// "5 moved, 1 cancelled, 2 left as they were" -- and what happened to the notices.
window.closureSummaryLine = function (counts, notices) {
  var parts = [];
  if (counts.moved) parts.push(counts.moved + " moved");
  if (counts.cancelled) parts.push(counts.cancelled + " cancelled");
  if (counts.left) parts.push(counts.left + " left as they were");
  if (counts.failed) parts.push(counts.failed + " could not be changed");
  var line = parts.length ? parts.join(", ") : "No appointments were touched";
  if (notices) {
    var told = [];
    if (notices.sent) told.push(notices.sent + " told on WhatsApp");
    if (notices.waiting) told.push(notices.waiting + " waiting to be sent (outside WhatsApp's 24-hour window)");
    if (notices.recorded) told.push(notices.recorded + " recorded, not sent (dry-run mode)");
    if (notices.failed) told.push(notices.failed + " could not be sent");
    if (told.length) line += ". " + told.join("; ");
  }
  return line + ".";
};

window.ClosureCard = (function () {
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function when(slot) { return window.cvDayLabel ? window.cvDayLabel(slot.date) + " " + slot.time : slot.date + " " + slot.time; }

  function scopeTitle(scope) {
    var title = "Close " + scope.branch + (scope.doctor ? " (" + scope.doctor + ")" : "");
    var days = scope.start_date === scope.end_date ? scope.start_date : scope.start_date + " to " + scope.end_date;
    var hours = scope.start_time ? ", " + scope.start_time + "-" + scope.end_time : "";
    return title + " · " + days + hours;
  }

  function build(plan, opts) {
    opts = opts || {};
    var card = el("div", "closure-card");
    card.appendChild(el("h3", "closure-title", scopeTitle(plan.scope)));
    var counts = plan.counts || { total: plan.moves.length, movable: 0, unresolved: 0 };
    card.appendChild(el("div", "muted", counts.total
      ? counts.total + " booked patient" + (counts.total === 1 ? "" : "s") + " in this window · "
        + counts.movable + " can be moved to a free slot" + (counts.unresolved ? ", " + counts.unresolved + " need a decision" : "")
      : "Nobody is booked in this window. Applying only stops new bookings."));

    var reasonInput = el("input"); reasonInput.type = "text"; reasonInput.maxLength = 200;
    reasonInput.placeholder = "Reason shown to patients, e.g. Doctor on leave"; reasonInput.value = opts.reason || "";
    var messageInput = el("input"); messageInput.type = "text"; messageInput.maxLength = 300;
    messageInput.placeholder = "Optional extra line for patients"; messageInput.value = opts.message || "";
    var fields = el("div", "closure-fields");
    [["Reason", reasonInput], ["Message to patients", messageInput]].forEach(function (pair) {
      var label = el("label"); label.appendChild(el("span", null, pair[0])); label.appendChild(pair[1]); fields.appendChild(label);
    });
    card.appendChild(fields);

    var rows = [];
    if (plan.moves.length) {
      var table = el("table", "closure-table");
      var head = el("tr");
      ["Patient", "Now", "What to do", "Moves to"].forEach(function (h) { head.appendChild(el("th", null, h)); });
      table.appendChild(head);
      plan.moves.forEach(function (move) {
        var tr = el("tr");
        var who = el("td"); who.appendChild(el("strong", null, move.name || "(no name)"));
        if (move.phone) who.appendChild(el("div", "muted", move.phone));
        tr.appendChild(who);
        tr.appendChild(el("td", null, move.from.branch + " · " + when(move.from)));
        var action = el("select");
        [["move", "Move"], ["cancel", "Cancel appointment"], ["leave", "Leave as is"]].forEach(function (a) {
          var o = el("option", null, a[1]); o.value = a[0]; if (move.action === a[0]) o.selected = true; action.appendChild(o);
        });
        var target = el("select");
        var none = el("option", null, "Choose…"); none.value = ""; target.appendChild(none);
        (move.options || []).forEach(function (option) {
          var o = el("option", null, option.label + (option.doctor ? " · " + option.doctor : ""));
          o.value = window.closureOptionValue(option); target.appendChild(o);
        });
        var chosen = window.closureOptionValue(move.to);
        if (move.to && !Array.prototype.some.call(target.options, function (o) { return o.value === chosen; })) {
          var extra = el("option", null, move.to.branch + " · " + move.to.date + " " + move.to.time); extra.value = chosen; target.appendChild(extra);
        }
        target.value = chosen;
        function sync() { target.disabled = action.value !== "move"; tr.classList.toggle("closure-leave", action.value === "leave"); }
        action.addEventListener("change", sync); sync();
        var a1 = el("td"); a1.appendChild(action);
        var a2 = el("td"); a2.appendChild(target);
        if (move.note) a2.appendChild(el("div", "muted closure-note", move.note));
        tr.appendChild(a1); tr.appendChild(a2);
        table.appendChild(tr);
        rows.push({ move: move, action: action, target: target });
      });
      var wrap = el("div", "closure-table-wrap"); wrap.appendChild(table); card.appendChild(wrap);
    }

    var status = el("div", "closure-status"); status.hidden = true;
    var actions = el("div", "closure-actions");
    var apply = el("button", "btn-confirm", "Apply"); apply.type = "button";
    var cancel = el("button", "btn-reject", "Cancel"); cancel.type = "button";
    actions.appendChild(apply); actions.appendChild(cancel);

    function chosenMoves() {
      return rows.map(function (r) { return window.closureRowPayload(r.move.appointment_id, r.action.value, r.target.value); });
    }
    function refreshLabel() {
      var n = rows.filter(function (r) { return r.action.value !== "leave"; }).length;
      apply.textContent = n ? "Apply to " + n + " patient" + (n === 1 ? "" : "s") + " and close" : "Close (stop new bookings)";
    }
    rows.forEach(function (r) { r.action.addEventListener("change", refreshLabel); });
    refreshLabel();

    function say(text, ok) { status.hidden = false; status.className = "closure-status " + (ok ? "closure-ok" : "closure-bad"); status.textContent = text; }

    apply.addEventListener("click", function () {
      var moves = chosenMoves();
      for (var i = 0; i < moves.length; i++) {
        if (moves[i].action === "move" && moves[i].to_branch_id === undefined) {
          say("Choose where to move " + (rows[i].move.name || "that patient") + ", or change what to do.", false); return;
        }
      }
      if (!window.confirm("Close " + plan.scope.branch + " and notify " + moves.filter(function (m) { return m.action !== "leave"; }).length + " patient(s)?")) return;
      apply.disabled = true; cancel.disabled = true;
      var body = Object.assign({}, plan.scope, { reason: reasonInput.value, message: messageInput.value, moves: moves });
      fetch(opts.applyUrl || "/closures/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) { return r.json(); })
        .then(function (result) {
          if (!result.ok) { say(result.error || "Could not apply the closure.", false); apply.disabled = false; cancel.disabled = false; return; }
          finished(result);
        })
        .catch(function (err) { say("Could not apply the closure: " + err.message, false); apply.disabled = false; cancel.disabled = false; });
    });

    function finished(result) {
      Array.prototype.forEach.call(card.querySelectorAll("select,input"), function (n) { n.disabled = true; });
      actions.innerHTML = "";
      say(window.closureSummaryLine(result.counts, result.notices), true);
      (result.results || []).filter(function (r) { return r.result === "failed"; }).forEach(function (r) {
        status.appendChild(el("div", "closure-fail", r.name + ": " + r.error + " — handle this one from the Queue tab."));
      });
      (result.left || []).forEach(function (l) {
        status.appendChild(el("div", "muted", l.name + " (" + l.date + " " + l.time + ") was left as booked — handle from the Queue tab."));
      });
      var undo = el("button", "btn-queue", "Undo this closure"); undo.type = "button";
      undo.addEventListener("click", function () {
        if (!window.confirm("Put everyone back and reopen " + plan.scope.branch + "?")) return;
        undo.disabled = true;
        fetch("/closures/" + result.closure_id + "/undo", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
          .then(function (r) { return r.json(); })
          .then(function (u) { say(u.message || u.error || "Done.", !!u.ok); if (u.ok) undo.remove(); else undo.disabled = false; if (opts.onChange) opts.onChange(); });
      });
      actions.appendChild(undo);
      if (opts.onDone) opts.onDone(result);
      if (window.DashboardRefresh) window.DashboardRefresh.refresh();
    }

    cancel.addEventListener("click", function () { if (opts.onCancel) opts.onCancel(); else card.remove(); });
    card.appendChild(status);
    card.appendChild(actions);
    return card;
  }

  return { build: build };
})();

// ---- the Automation tab section ---------------------------------------------
document.addEventListener("DOMContentLoaded", function () {
  var section = document.getElementById("closures-card");
  if (!section) return;
  var form = document.getElementById("closure-form");
  var review = document.getElementById("closure-review");
  var list = document.getElementById("closures-list");
  var branchSel = document.getElementById("closure-branch");
  var doctorSel = document.getElementById("closure-doctor");
  var error = document.getElementById("closure-error");

  function opt(select, value, text) { var o = document.createElement("option"); o.value = value; o.textContent = text; select.appendChild(o); }

  function fill() {
    if (!window.Branches) return;
    var multi = Branches.multi();
    section.hidden = !multi;
    if (!multi) return;
    var b = branchSel.value, d = doctorSel.value;
    branchSel.innerHTML = ""; doctorSel.innerHTML = "";
    Branches.list().forEach(function (x) { opt(branchSel, x.id, x.name); });
    opt(doctorSel, "", "Whole branch");
    (Branches.data().doctors || []).forEach(function (x) { opt(doctorSel, x.id, x.name + " only"); });
    branchSel.value = b || String(Branches.mine()); doctorSel.value = d;
  }
  document.addEventListener("branchdata", fill);
  fill();

  function fail(message) { error.hidden = !message; error.textContent = message || ""; }

  function body() {
    return {
      branch_id: parseInt(branchSel.value, 10), doctor_id: doctorSel.value ? parseInt(doctorSel.value, 10) : null,
      start_date: document.getElementById("closure-start-date").value, end_date: document.getElementById("closure-end-date").value,
      start_time: document.getElementById("closure-start-time").value, end_time: document.getElementById("closure-end-time").value,
    };
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    fail("");
    review.innerHTML = "";
    fetch("/closures/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body()) })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (!r.ok) { fail(r.error || "Could not prepare the closure."); return; }
        review.appendChild(ClosureCard.build(r.plan, {
          reason: document.getElementById("closure-reason").value,
          message: document.getElementById("closure-message").value,
          onCancel: function () { review.innerHTML = ""; },
          onDone: function () { load(); }, onChange: function () { load(); },
        }));
      })
      .catch(function (err) { fail("Could not prepare the closure: " + err.message); });
  });

  function renderHistory(items) {
    list.innerHTML = "";
    if (!items.length) { var p = document.createElement("p"); p.className = "muted"; p.textContent = "No closures yet."; list.appendChild(p); return; }
    items.forEach(function (c) {
      var row = document.createElement("div"); row.className = "closure-history-row";
      var title = document.createElement("div"); title.className = "closure-history-title";
      var days = c.start_date === c.end_date ? c.start_date : c.start_date + " to " + c.end_date;
      title.textContent = c.branch + (c.doctor ? " (" + c.doctor + ")" : "") + " · " + days + (c.reason ? " · " + c.reason : "") + (c.status === "undone" ? " · undone" : "");
      row.appendChild(title);
      var k = c.counts;
      var line = document.createElement("div"); line.className = "muted";
      line.textContent = c.status === "undone"
        ? k.undone + " put back" + (k.skipped ? ", " + k.skipped + " left as they were (the patient had changed them, or the slot was gone)" : "")
        : k.moved + " moved, " + k.cancelled + " cancelled" + (k.failed ? ", " + k.failed + " failed" : "")
          + " · patients: " + k.accepted + " accepted, " + k.changed + " chose another, " + k.waiting + " not answered";
      row.appendChild(line);
      if (c.moves.length) {
        var details = document.createElement("details");
        var summary = document.createElement("summary"); summary.textContent = "Patients"; details.appendChild(summary);
        var ul = document.createElement("ul"); ul.className = "block-affected";
        c.moves.forEach(function (m) {
          var li = document.createElement("li");
          var to = m.action === "move" ? " → " + m.to_branch + " " + m.to_date + " " + m.to_time : " → cancelled";
          li.textContent = (m.name || "(no name)") + ": " + m.from_branch + " " + m.from_date + " " + m.from_time + to
            + " [" + m.result + (m.response !== "none" ? ", " + m.response : "") + (m.error ? ": " + m.error : "") + "]";
          ul.appendChild(li);
        });
        details.appendChild(ul); row.appendChild(details);
      }
      if (c.status === "applied") {
        var undo = document.createElement("button"); undo.type = "button"; undo.className = "btn-queue"; undo.textContent = "Undo";
        undo.addEventListener("click", function () {
          if (!window.confirm("Put everyone back and reopen " + c.branch + "?")) return;
          undo.disabled = true;
          fetch("/closures/" + c.id + "/undo", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
            .then(function (r) { return r.json(); })
            .then(function (u) { fail(u.ok ? "" : (u.error || "Could not undo.")); load(); if (window.DashboardRefresh) window.DashboardRefresh.refresh(); });
        });
        row.appendChild(undo);
      }
      list.appendChild(row);
    });
  }

  function load() {
    return fetch("/closures/data").then(function (r) { return r.json(); }).then(function (r) { if (r.ok) renderHistory(r.closures); });
  }
  document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "automation") load(); });
  load();
});

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    closureOptionValue: window.closureOptionValue, closureParseValue: window.closureParseValue,
    closureRowPayload: window.closureRowPayload, closureSummaryLine: window.closureSummaryLine,
  };
}
