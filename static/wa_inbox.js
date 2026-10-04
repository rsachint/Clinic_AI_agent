document.addEventListener("DOMContentLoaded", function () {
  var container = document.getElementById("wa-inbox-list");
  if (!container) return;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  // Why the WhatsApp conversation agent (clinic/conversation.py) passed a
  // message to a person. slots.flag says what kind; slots.reason says why.
  var FLAG_TEXT = {
    emergency: "EMERGENCY -- the patient was told to call 112 / 108 and that the clinic was alerted. Contact them now.",
    clinical: "Clinical question -- the patient was told we cannot give medical advice on chat. Please call them back.",
    escalation: "The patient needs a person. Reply to them directly (or open the conversation below and Take over).",
    human_mode: "This conversation is in human mode (you took over) -- the assistant is silent. Reply to this patient.",
  };
  var REASON_TEXT = {
    human_request: "They asked to talk to a person.",
    confusion: "The assistant could not understand them after two tries.",
    rate_limit: "They sent more than 30 messages in an hour.",
    no_availability: "No free slots in the next 30 days.",
    register: "They want to register as a new patient.",
    status_unavailable: "They asked about their token but it could not be answered.",
  };

  function renderItem(item) {
    var flag = item.slots && item.slots.flag;
    var wrap = el("div", { class: "proposal" + (flag === "emergency" ? " wa-emergency" : ""), style: "flex-direction:column;align-items:stretch;" });
    if (flag === "emergency") {
      wrap.appendChild(el("div", { class: "wa-emergency-banner", text: "EMERGENCY" }));
    }
    wrap.appendChild(el("div", { class: "muted", style: "font-size:12px;", text: "From " + item.wa_id + " -- " + (window.formatIST ? window.formatIST(item.received_at) : item.received_at) + " IST" }));
    wrap.appendChild(el("div", { style: "margin:6px 0 10px;", text: item.raw_text || "(no text)" }));

    if (item.status === "needs_human_reply" || item.status === "error") {
      if (item.status === "error") {
        wrap.appendChild(el("div", { class: "flash error", style: "margin-bottom:10px;", text: "Processing error: " + (item.error_text || "unknown") }));
      } else if (flag) {
        var why = FLAG_TEXT[flag] || "Needs a person.";
        var reason = item.slots.reason && REASON_TEXT[item.slots.reason];
        wrap.appendChild(el("div", { class: flag === "emergency" ? "flash error" : "field-note", style: "margin-bottom:10px;", text: why + (reason && flag !== "emergency" ? " " + reason : "") }));
      } else {
        wrap.appendChild(el("div", { class: "muted", style: "margin-bottom:10px;", text: "Could not classify -- reply to this patient directly on WhatsApp, then dismiss." }));
      }
      var dismissBtn = el("button", { class: "btn-reject", type: "button", text: "Dismiss" });
      dismissBtn.addEventListener("click", function () {
        fetch("/wa/" + item.id + "/dismiss", { method: "POST" })
          .then(function () { wrap.remove(); });
      });
      wrap.appendChild(dismissBtn);
      container.appendChild(wrap);
      return;
    }

    // classified: reuse the shared structured review card
    var notes = [];
    if (item.slots && item.slots.needs_staff_reason) {
      // The automatic path (clinic/auto_policy.py) declined this request: say why.
      notes.push("Needs staff: " + item.slots.needs_staff_reason);
    }
    if (item.slots && item.slots.suggestion_note) {
      // A pre-filled date/time the patient never asked for (see
      // clinic/whatsapp_pipeline.py suggest_slot): make it unmistakable.
      notes.push(item.slots.suggestion_note);
    }
    if (item.slots && item.slots.agent_note) {
      // Collected by the WhatsApp conversation agent: the patient chose and
      // confirmed this themselves, and the slot is held for them.
      notes.push(item.slots.agent_note);
    }
    var card = ReviewCard.build(
      { intent: item.intent, slots: item.slots, resolved: { patient_id: item.slots && item.slots.patient_id ? item.slots.patient_id : item.patient_id } },
      {
        approveUrl: "/wa/" + item.id + "/approve",
        notes: notes,
        context: { followups: item.pending_followups || [], appointments: item.appointments || [] },
        buildApprovePayload: function (slots) {
          return { intent: item.intent, slots: slots };
        },
        onApproved: function (result) {
          if (!result.ok) {
            wrap.appendChild(el("div", { class: "flash error", text: "Could not save: " + result.error }));
            return;
          }
          ReviewCard.showOutcome(card, "✅ " + result.message, true);
          // The approved item drops out of the server's wa_inbox query on
          // its own (status is no longer classified/needs_human_reply/
          // error), so a plain re-fetch-and-re-render removes this row --
          // no location.reload(), which would also reset any other open
          // tab's in-progress state.
          setTimeout(function () {
            if (window.DashboardRefresh) DashboardRefresh.refresh();
          }, 900);
        },
        onRejected: function () {
          fetch("/wa/" + item.id + "/reject", { method: "POST" })
            .then(function () {
              ReviewCard.showOutcome(card, "Rejected -- nothing was saved.", true);
              setTimeout(function () {
                if (window.DashboardRefresh) DashboardRefresh.refresh();
              }, 900);
            });
        },
      }
    );
    card.classList.remove("card"); // avoid a card-inside-a-card border; wrap already has one
    wrap.appendChild(card);
    container.appendChild(wrap);
  }

  function render(items) {
    container.innerHTML = "";
    if (!items || items.length === 0) {
      container.appendChild(el("p", { class: "muted", text: "No patient messages waiting." }));
    } else {
      items.forEach(renderItem);
    }
  }

  // Exposed so static/dashboard_refresh.js can re-render this list in place
  // with fresh data after an approve/reject/dismiss, instead of reloading
  // the whole page (see that file's comment for why).
  window.WaInbox = { render: render };

  var initialItems = JSON.parse(document.getElementById("wa-inbox-data").textContent || "[]");
  render(initialItems);
});
