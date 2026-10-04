// Patient messages tab -> "Conversations": one collapsible thread per
// WhatsApp sender (what they wrote, what the assistant / clinic sent back),
// the agent/human mode with Take over / Resume agent, and a plain reply box.
//
// All text from patients is put on the page with textContent only.
// Re-rendering (a poll, or DashboardRefresh after an approve) keeps which
// threads are open, the scroll position of each transcript, and any
// half-typed reply, so it never eats what staff are writing.
document.addEventListener("DOMContentLoaded", function () {
  var container = document.getElementById("wa-threads-list");
  if (!container) return;
  var countEl = document.getElementById("wa-threads-count");
  var section = document.querySelector('.tab-panel[data-tab="messages"]');

  var POLL_MS = 10000;
  var openThreads = {};   // wa_id -> true when its <details> is open
  var drafts = {};        // wa_id -> unsent reply text
  var notes = {};         // wa_id -> {text, ok} last result of Send
  var lastThreads = [];

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function when(utc) {
    if (!utc) return "";
    var d = new Date(String(utc).replace(" ", "T") + "Z");   // the database stores UTC
    if (isNaN(d.getTime())) return utc;
    return d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  var STATUS_LABELS = {
    sent: "sent", dry_run: "dry run (not sent)", pending: "sending...",
    blocked_no_window: "blocked: 24-hour WhatsApp window closed", failed: "failed",
    skipped_no_phone: "no phone number",
  };
  var EVENT_LABELS = {
    conv_reply: "assistant", staff_message: "staff", status_reply: "token update", request_declined: "declined notice",
    booking_confirmed: "booking confirmed", appointment_rescheduled: "rescheduled", appointment_cancelled: "cancelled",
    token_changed: "token changed", reminder_day_before: "reminder", reminder_morning: "reminder",
    queue_two_ahead: "queue update", your_turn: "your turn", registered: "registered",
  };
  var FLAG_LABELS = {
    emergency: "EMERGENCY", clinical: "clinical question", escalation: "needs a person", human_mode: "human mode",
  };

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  function bubble(item) {
    var out = item.dir === "out";
    var cls = "wa-bubble " + (out ? "wa-bubble-out" : "wa-bubble-in");
    if (item.flag === "emergency") cls += " wa-bubble-emergency";
    var node = el("div", { class: cls });
    var label = out ? (EVENT_LABELS[item.event] || item.event) : (item.voice ? "voice note" : "patient");
    node.appendChild(el("div", { class: "wa-bubble-meta", text: label + " · " + when(item.at) }));
    node.appendChild(el("div", { class: "wa-bubble-text", text: item.text || "(no text)" }));
    if (out && item.options && item.options.length) {
      var opts = el("div", { class: "wa-bubble-options" });
      item.options.forEach(function (title) { opts.appendChild(el("span", { class: "wa-chip", text: title })); });
      node.appendChild(opts);
    }
    if (!out && item.flag) {
      node.appendChild(el("div", { class: "wa-bubble-flag", text: FLAG_LABELS[item.flag] || item.flag }));
    }
    if (out) {
      var status = el("div", { class: "wa-bubble-status notif-badge notif-" + item.status, text: STATUS_LABELS[item.status] || item.status });
      node.appendChild(status);
      if (item.error && item.status !== "blocked_no_window") {
        node.appendChild(el("div", { class: "muted wa-bubble-error", text: item.error }));
      }
      if (item.retry) {
        var retry = el("button", { type: "button", class: "btn-queue", text: "Retry" });
        retry.addEventListener("click", function () {
          retry.disabled = true;
          post("/notifications/" + item.id + "/retry").then(function () { refresh(true); });
        });
        node.appendChild(retry);
      }
    }
    return node;
  }

  function renderThread(t) {
    var details = el("details", { class: "wa-thread" + (t.emergency ? " wa-thread-emergency" : ""), "data-wa-id": t.wa_id });
    if (openThreads[t.wa_id] || (openThreads[t.wa_id] === undefined && t.emergency)) details.setAttribute("open", "open");
    details.addEventListener("toggle", function () { openThreads[t.wa_id] = details.open; });

    var summary = el("summary", { class: "wa-thread-head" });
    summary.appendChild(el("span", { class: "wa-thread-name", text: t.name || "Unregistered" }));
    summary.appendChild(el("span", { class: "muted wa-thread-phone", text: t.wa_id }));
    if (t.emergency) summary.appendChild(el("span", { class: "wa-badge wa-badge-emergency", text: "EMERGENCY" }));
    summary.appendChild(el("span", {
      class: "wa-badge wa-badge-mode " + (t.mode === "human" ? "wa-badge-human" : "wa-badge-agent"),
      text: t.mode === "human" ? "Human" : "Agent",
    }));
    if (t.open_items) summary.appendChild(el("span", { class: "wa-badge wa-badge-open", text: t.open_items + " waiting" }));
    summary.appendChild(el("span", { class: "muted wa-thread-when", text: when(t.last_at) }));
    details.appendChild(summary);

    var transcript = el("div", { class: "wa-transcript" });
    (t.items || []).forEach(function (item) { transcript.appendChild(bubble(item)); });
    details.appendChild(transcript);

    var controls = el("div", { class: "wa-thread-controls" });
    var toggle = el("button", {
      type: "button", class: "btn-queue " + (t.mode === "human" ? "btn-queue-primary" : ""),
      text: t.mode === "human" ? "Resume agent" : "Take over",
    });
    toggle.addEventListener("click", function () {
      toggle.disabled = true;
      post("/wa/threads/" + encodeURIComponent(t.wa_id) + "/mode", { mode: t.mode === "human" ? "agent" : "human" })
        .then(function () { refresh(true); });
    });
    controls.appendChild(toggle);
    controls.appendChild(el("span", {
      class: "muted wa-window " + (t.window_open ? "" : "wa-window-closed"),
      text: t.window_open ? "24-hour window open: you can reply here" : "24-hour window closed: WhatsApp will not deliver a free-form message",
    }));
    details.appendChild(controls);

    var form = el("div", { class: "wa-reply" });
    var box = el("textarea", { rows: "2", maxlength: "1000", placeholder: "Type a reply to this patient...", "data-wa-draft": t.wa_id });
    box.value = drafts[t.wa_id] || "";
    box.addEventListener("input", function () { drafts[t.wa_id] = box.value; });
    var send = el("button", { type: "button", class: "btn-confirm", text: "Send" });
    var note = el("div", { class: "wa-reply-note" });
    if (notes[t.wa_id]) {
      note.textContent = notes[t.wa_id].text;
      note.className = "wa-reply-note " + (notes[t.wa_id].ok ? "wa-note-ok" : "wa-note-bad");
    }
    send.addEventListener("click", function () {
      var text = box.value.trim();
      if (!text) { return; }
      send.disabled = true;
      post("/wa/threads/" + encodeURIComponent(t.wa_id) + "/reply", { text: text })
        .then(function (result) {
          if (!result.ok) {
            notes[t.wa_id] = { ok: false, text: result.error || "Could not send." };
          } else if (result.status === "blocked_no_window") {
            notes[t.wa_id] = { ok: false, text: "Blocked: this patient has not written in the last 24 hours, so WhatsApp will not deliver it. Call or use WhatsApp directly." };
          } else if (result.status === "failed") {
            notes[t.wa_id] = { ok: false, text: "Sending failed: " + (result.error || "unknown error") + " (Retry is in the thread)." };
          } else {
            notes[t.wa_id] = { ok: true, text: result.status === "dry_run" ? "Recorded (dry run: nothing was sent)." : "Sent." };
            drafts[t.wa_id] = "";
          }
          refresh(true);
        })
        .catch(function (err) { notes[t.wa_id] = { ok: false, text: "Request failed: " + err }; refresh(true); });
    });
    form.appendChild(box);
    form.appendChild(el("div", { class: "wa-reply-actions" }, [send, note]));
    details.appendChild(form);

    return details;
  }

  function render(threads) {
    lastThreads = threads || [];
    // Keep each transcript's scroll position (or stay pinned to the newest
    // message when it was already at the bottom).
    var scroll = {};
    container.querySelectorAll(".wa-thread").forEach(function (node) {
      var tr = node.querySelector(".wa-transcript");
      if (tr) scroll[node.getAttribute("data-wa-id")] = { top: tr.scrollTop, atEnd: tr.scrollTop + tr.clientHeight >= tr.scrollHeight - 4 };
    });
    container.innerHTML = "";
    if (countEl) countEl.textContent = "(" + lastThreads.length + ")";
    if (!lastThreads.length) {
      container.appendChild(el("p", { class: "muted", text: "No conversations yet." }));
      return;
    }
    lastThreads.forEach(function (t) {
      var node = renderThread(t);
      container.appendChild(node);
      var tr = node.querySelector(".wa-transcript");
      var s = scroll[t.wa_id];
      if (tr) tr.scrollTop = (!s || s.atEnd) ? tr.scrollHeight : s.top;
    });
  }

  function typing() {
    var a = document.activeElement;
    return a && a.tagName === "TEXTAREA" && container.contains(a) && a.value !== "";
  }

  // `force` re-renders even while someone is typing (the draft is restored);
  // the background poll skips the render instead of risking the caret.
  function refresh(force) {
    return fetch("/wa/threads", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (threads) { if (force === true || !typing()) render(threads); })
      .catch(function () { /* keep showing the last good threads */ });
  }

  window.WaThreads = { render: render, refresh: refresh };

  var initial = [];
  try { initial = JSON.parse(document.getElementById("wa-threads-data").textContent || "[]"); } catch (e) { initial = []; }
  render(initial);

  document.addEventListener("tabchange", function (event) {
    if (event.detail && event.detail.id === "messages") refresh();
  });
  setInterval(function () {
    if (section && !section.hidden && document.visibilityState === "visible") refresh();
  }, POLL_MS);
});
