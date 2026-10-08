// Audit log tab: "Questions I couldn't answer" -- the questions staff asked the Assistant that
// no record type can answer yet (clinic/unanswered.py). A developer adds support for one, then
// staff mark it as added with a one-line note ("Ask: who is on duty now"); the Assistant then
// tells the user once that it works (static/unanswered_notice.js). Nothing here writes clinic
// data, and text from the server is always put in with textContent, never as HTML.
(function () {
  var STATUS_LABELS = { "new": "New", building: "Being built", resolved: "Added", dismissed: "Dismissed" };

  // Which buttons a question in `status` offers: Mark as added (asks for a note), Mark as building,
  // Dismiss, and Reopen for a question that was added or dismissed.
  function actionsFor(status) {
    if (status === "new") return ["resolved", "building", "dismissed"];
    if (status === "building") return ["resolved", "dismissed"];
    return ["new"];
  }
  var ACTION_LABELS = { resolved: "Mark as added", building: "Mark as building", dismissed: "Dismiss", "new": "Reopen" };

  function askedText(item) {
    return item.times_asked === 1 ? "once" : item.times_asked + " times";
  }
  function minute(stamp) { return stamp ? String(stamp).slice(0, 16) : "-"; }

  var api = { actionsFor: actionsFor, actionLabel: function (s) { return ACTION_LABELS[s]; }, askedText: askedText, statusLabel: function (s) { return STATUS_LABELS[s] || s; } };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  window.UnansweredCard = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("unanswered-card");
    if (!root) return;
    var savedId = null;        // the question just marked as added: its row gets the check mark after the redraw

    function el(tag, cls, text) {
      var node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }
    function post(url, body) {
      return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(function (r) { return r.json(); });
    }
    function setStatus(item, status, note, onError) {
      post("/unanswered/" + item.id + "/status", { status: status, note: note || null }).then(function (r) {
        if (!r.ok) { onError(r.error || "Could not save."); return; }
        savedId = status === "resolved" ? item.id : null;
        load();
      }).catch(function () { onError("Could not save."); });
    }

    function actionButtons(item, cell) {
      var message = el("span", "save-status error");
      message.setAttribute("role", "status");
      actionsFor(item.status).forEach(function (target) {
        var button = el("button", "btn" + (target === "resolved" ? " btn-confirm" : ""), ACTION_LABELS[target]);
        button.type = "button";
        button.addEventListener("click", function () {
          if (target === "resolved") { noteForm(item, cell); return; }
          setStatus(item, target, null, function (text) { message.textContent = text; });
        });
        cell.appendChild(button);
        cell.appendChild(document.createTextNode(" "));
      });
      cell.appendChild(message);
    }

    // "Mark as added" asks what the user can now ask, in one line: that line is what they are told.
    function noteForm(item, cell) {
      cell.textContent = "";
      var input = el("input");
      input.type = "text";
      input.maxLength = 200;
      input.placeholder = "What can they ask now? e.g. Ask: who is on duty now";
      input.setAttribute("aria-label", "What can they ask now");
      input.className = "unanswered-note";
      var save = el("button", "btn btn-confirm", "Save");
      save.type = "button";
      var cancel = el("button", "btn", "Cancel");
      cancel.type = "button";
      var message = el("span", "save-status error");
      message.setAttribute("role", "status");
      save.addEventListener("click", function () {
        var note = input.value.trim();
        if (!note) { message.textContent = "Write one line about what can be asked now."; return; }
        setStatus(item, "resolved", note, function (text) { message.textContent = text; });
      });
      cancel.addEventListener("click", load);
      cell.appendChild(input);
      cell.appendChild(document.createTextNode(" "));
      cell.appendChild(save);
      cell.appendChild(document.createTextNode(" "));
      cell.appendChild(cancel);
      cell.appendChild(message);
      input.focus();
    }

    function table(items, closed) {
      var t = el("table");
      var head = el("tr");
      ["Question", "Asked", "Last asked", "Status", ""].forEach(function (h) { head.appendChild(el("th", null, h)); });
      t.appendChild(head);
      items.forEach(function (item) {
        var tr = el("tr");
        var q = el("td", "unanswered-question", item.question);
        if (item.wanted) q.appendChild(el("div", "muted", "Wanted: " + item.wanted));
        if (closed && item.resolved_note) q.appendChild(el("div", "muted", "Note: " + item.resolved_note));
        tr.appendChild(q);
        tr.appendChild(el("td", null, askedText(item)));
        tr.appendChild(el("td", null, minute(item.last_asked_at)));
        tr.appendChild(el("td", null, STATUS_LABELS[item.status] || item.status));
        var actions = el("td", "unanswered-actions");
        actionButtons(item, actions);
        tr.appendChild(actions);
        t.appendChild(tr);
        if (savedId === item.id) {
          var again = actions.querySelector("button");
          if (again && window.SaveTick) window.SaveTick.show(again);
          savedId = null;
        }
      });
      return t;
    }

    function draw(data) {
      root.textContent = "";
      var card = el("div", "card");
      var h = el("h2", null, "Questions I couldn't answer ");
      h.appendChild(el("span", "count", "(" + data.open.length + " open)"));
      card.appendChild(h);
      card.appendChild(el("p", "muted",
        "Things staff asked the Assistant that it cannot read yet. A developer adds support for each one (see the README); " +
        "mark it as added with a one-line note and the person who asked is told once that it works. " +
        "The questions are kept on this computer only and may contain patient names."));
      if (data.open.length) card.appendChild(table(data.open, false));
      else card.appendChild(el("p", "muted", "No open questions."));
      if (data.closed.length) {
        var details = el("details", "unanswered-closed");
        details.appendChild(el("summary", null, "Added or dismissed (" + data.closed.length + ")"));
        details.appendChild(table(data.closed, true));
        if (savedId !== null) details.open = true;
        card.appendChild(details);
      }
      root.appendChild(card);
    }

    function load() {
      fetch("/unanswered/data").then(function (r) { return r.json(); }).then(function (r) { if (r.ok) draw(r); })
        .catch(function () { /* the card keeps what it had */ });
    }

    document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "audit") load(); });
    load();
  });
})();
