// Shared structured-review-card rendering, used by both the voice flow
// (static/speak.js) and the WhatsApp inbox (static/wa_inbox.js). One
// FIELD_SPECS-driven renderer, not two parallel UI systems.
var ReviewCard = (function () {
  var PATIENTS = JSON.parse(document.getElementById("patients-data").textContent || "[]");
  var STAFF = JSON.parse(document.getElementById("staff-data").textContent || "[]");

  // Called after an /approve commits a write (e.g. register_patient) so the
  // *next* card's patient/staff dropdowns include it, without a page reload.
  // See static/dashboard_refresh.js, the caller.
  function setData(patients, staff) {
    PATIENTS = patients || PATIENTS;
    STAFF = staff || STAFF;
  }

  var INTENT_LABELS = {
    register_patient: "Register patient",
    register_staff: "Register staff",
    record_visit: "Log a visit",
    set_followup: "Set follow-up",
    log_attendance: "Log attendance",
    log_expense: "Log expense",
    confirm_followup: "Confirm follow-up",
    cancel_followup: "Cancel follow-up",
    reschedule_followup: "Reschedule follow-up",
    book_appointment: "Book appointment",
    cancel_appointment: "Cancel appointment",
    reschedule_appointment: "Reschedule appointment",
    queue_check_in: "Check in patient",
    queue_call_next: "Call patient in",
    queue_mark_done: "Mark consultation done",
    queue_mark_no_show: "Mark no-show",
  };

  // type: text/number -> plain input. select -> fixed options. patient/staff
  // -> a dropdown from the page-wide PATIENTS/STAFF lists. followup -> a
  // dropdown from that specific item's own pending follow-ups (passed in
  // per-call via `context.followups`, since unlike patients/staff there's
  // no single global list -- it's specific to one patient).
  var FIELD_SPECS = {
    register_patient: [
      { key: "name", label: "Name", type: "text" },
      { key: "phone", label: "Phone", type: "text" },
      { key: "age", label: "Age", type: "number" },
    ],
    register_staff: [
      { key: "name", label: "Name", type: "text" },
      { key: "role", label: "Role", type: "text" },
    ],
    record_visit: [
      { key: "patient_id", label: "Patient", type: "patient" },
      { key: "fee_rupees", label: "Fee (Rs)", type: "number" },
    ],
    set_followup: [
      { key: "patient_id", label: "Patient", type: "patient" },
      { key: "days_from_now", label: "Days from now", type: "number" },
    ],
    log_attendance: [
      { key: "staff_id", label: "Staff", type: "staff" },
      { key: "status", label: "Status", type: "select", options: ["present", "half_day", "absent", "leave"] },
    ],
    log_expense: [
      { key: "description", label: "Description", type: "text" },
      { key: "amount_rupees", label: "Amount (Rs)", type: "number" },
    ],
    confirm_followup: [
      { key: "followup_id", label: "Follow-up", type: "followup" },
    ],
    cancel_followup: [
      { key: "followup_id", label: "Follow-up", type: "followup" },
    ],
    reschedule_followup: [
      { key: "followup_id", label: "Follow-up", type: "followup" },
      { key: "new_due_date", label: "New due date (YYYY-MM-DD)", type: "text" },
    ],
    book_appointment: [
      { key: "patient_id", label: "Existing patient (if registered)", type: "patient" },
      { key: "patient_name", label: "Patient name (if not registered)", type: "text" },
      { key: "patient_phone", label: "Patient phone (if not registered)", type: "text" },
      { key: "branch_id", label: "Branch", type: "branch" },
      { key: "appt_date", label: "Date (YYYY-MM-DD)", type: "text" },
      { key: "start_time", label: "Start time (HH:MM)", type: "text" },
      { key: "notes", label: "Notes", type: "text" },
    ],
    // `detail`: the settled appointment is shown as text (the calendar hover's details), not a dropdown.
    cancel_appointment: [
      { key: "appointment_id", label: "Appointment", type: "appointment", detail: true },
    ],
    reschedule_appointment: [
      { key: "appointment_id", label: "Appointment", type: "appointment", detail: true },
      { key: "branch_id", label: "Move to branch", type: "branch", keep: true },
      { key: "appt_date", label: "New date (YYYY-MM-DD)", type: "text" },
      { key: "start_time", label: "New start time (HH:MM)", type: "text" },
    ],
    // Day-of queue actions. The dropdown lists today's outstanding
    // appointments by token (context.appointments, labelled "T-04 Name
    // 09:15"), so a misheard token or name is visible and fixable here.
    queue_check_in: [
      { key: "appointment_id", label: "Patient (token)", type: "appointment" },
    ],
    queue_call_next: [
      { key: "appointment_id", label: "Patient (token)", type: "appointment" },
    ],
    queue_mark_done: [
      { key: "appointment_id", label: "Patient (token)", type: "appointment" },
    ],
    queue_mark_no_show: [
      { key: "appointment_id", label: "Patient (token)", type: "appointment" },
    ],
  };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  // The Cancel / Reschedule card's appointment: read-only text with the same details as the calendar's hover
  // (static/appt_details.js), not a dropdown. A hidden input carries the id (data-key), so gatherFields and a
  // voice edit work as for any field. A selector appears only when a choice is really open: nothing is picked yet
  // (several people or dates fit, or none was found), or the nearest of several was picked and another may be meant.
  // Choosing one fills in its details below.
  function buildAppointmentDetail(spec, slots, context, wrap) {
    var appointments = (context && context.appointments) || [];
    function find(id) {
      var found = null;
      appointments.forEach(function (a) { if (id !== "" && id != null && String(a.id) === String(id)) found = a; });
      return found;
    }
    var picked = find(slots[spec.key]);
    var carrier = el("input", { type: "hidden", "data-key": spec.key, value: picked ? picked.id : "" });
    var box = el("div", { class: "appt-details" });

    function render() {
      while (box.firstChild) box.removeChild(box.firstChild);
      var a = find(carrier.value);
      box.hidden = !a;
      if (!a) return;
      window.ApptDetails.lines(a).forEach(function (line) {
        box.appendChild(el("div", { class: "appt-line appt-" + line.kind, text: line.text }));
      });
    }

    wrap.appendChild(carrier);
    wrap.appendChild(box);
    if (!picked || appointments.length > 1) {
      var select = el("select", { class: "appt-choose", "aria-label": spec.label });
      if (!picked) select.appendChild(el("option", { value: "", selected: "selected" }, [document.createTextNode("Select...")]));
      appointments.forEach(function (a) {
        var opt = el("option", { value: a.id }, [document.createTextNode(window.ApptDetails.optionLabel(a))]);
        if (picked && String(picked.id) === String(a.id)) opt.setAttribute("selected", "selected");
        select.appendChild(opt);
      });
      select.addEventListener("change", function () { carrier.value = select.value; render(); });
      wrap.appendChild(select);
      carrier._sync = function () { select.value = carrier.value; };
    }
    // A voice edit that sets the carrier's value redraws the text (and moves the selector if there is one).
    carrier._refresh = function () { if (carrier._sync) carrier._sync(); render(); };
    render();
  }

  function buildFieldInput(spec, slots, resolved, context) {
    var wrap = el("div", { class: "field-row" });
    if (spec.type === "branch" && !(window.Branches && Branches.multi())) {
      // One branch: nothing to choose, but keep whatever the request carried.
      return el("input", { type: "hidden", "data-key": spec.key, value: slots[spec.key] == null ? "" : slots[spec.key] });
    }
    wrap.appendChild(el("label", { text: spec.label }));

    if (spec.type === "patient" || spec.type === "staff") {
      var select = el("select", { "data-key": spec.key });
      var options = spec.type === "patient" ? PATIENTS : STAFF;
      var preselectId = spec.type === "patient" ? resolved.patient_id : resolved.staff_id;
      select.appendChild(el("option", { value: "" }, [document.createTextNode("Select...")]));
      options.forEach(function (o) {
        var label = spec.type === "patient" ? (o.name + " (" + o.phone + ")") : o.name;
        var opt = el("option", { value: o.id }, [document.createTextNode(label)]);
        if (preselectId && String(preselectId) === String(o.id)) opt.setAttribute("selected", "selected");
        select.appendChild(opt);
      });
      wrap.appendChild(select);
    } else if (spec.type === "branch") {
      // The branch the request names (a patient's WhatsApp choice, or a
      // spoken one); otherwise My branch, shown so staff can change it.
      var branchSelect = el("select", { "data-key": spec.key });
      var named = slots[spec.key] != null && slots[spec.key] !== "";
      // A move keeps the appointment's own branch unless another is chosen.
      var chosen = named ? slots[spec.key] : (spec.keep ? "" : Branches.mine());
      if (spec.keep) {
        var keepOpt = el("option", { value: "" }, [document.createTextNode("Same branch as now")]);
        if (chosen === "") keepOpt.setAttribute("selected", "selected");
        branchSelect.appendChild(keepOpt);
      }
      Branches.list().forEach(function (b) {
        var opt = el("option", { value: b.id }, [document.createTextNode(b.name)]);
        if (String(chosen) === String(b.id)) opt.setAttribute("selected", "selected");
        branchSelect.appendChild(opt);
      });
      wrap.appendChild(branchSelect);
    } else if (spec.type === "followup") {
      var fuSelect = el("select", { "data-key": spec.key });
      var followups = (context && context.followups) || [];
      followups.forEach(function (f) {
        var opt = el("option", { value: f.id }, [document.createTextNode("Due " + f.due_date)]);
        if (slots[spec.key] && String(slots[spec.key]) === String(f.id)) opt.setAttribute("selected", "selected");
        fuSelect.appendChild(opt);
      });
      wrap.appendChild(fuSelect);
    } else if (spec.type === "appointment" && spec.detail && window.ApptDetails) {
      buildAppointmentDetail(spec, slots, context, wrap);
    } else if (spec.type === "appointment") {
      // Mirrors the "followup" branch above: options come from this
      // specific patient's own upcoming appointments (context.appointments),
      // not a global list -- same reasoning as followup's own comment.
      var apptSelect = el("select", { "data-key": spec.key });
      var appointments = (context && context.appointments) || [];
      // No best guess (e.g. a misheard token): force a deliberate choice
      // instead of silently defaulting to the first option, which for a
      // queue action would be the wrong patient.
      if (!slots[spec.key]) {
        apptSelect.appendChild(el("option", { value: "", selected: "selected" }, [document.createTextNode("Select...")]));
      }
      appointments.forEach(function (a) {
        // Queue intents supply a ready-made label ("T-04 Sunita 09:15");
        // follow-up/appointment intents show date + time.
        var label = a.label || (a.appt_date + " " + a.start_time);
        var opt = el("option", { value: a.id }, [document.createTextNode(label)]);
        if (slots[spec.key] && String(slots[spec.key]) === String(a.id)) opt.setAttribute("selected", "selected");
        apptSelect.appendChild(opt);
      });
      wrap.appendChild(apptSelect);
    } else if (spec.type === "select") {
      var sel = el("select", { "data-key": spec.key });
      spec.options.forEach(function (optValue) {
        var opt = el("option", { value: optValue }, [document.createTextNode(optValue)]);
        if (slots[spec.key] === optValue) opt.setAttribute("selected", "selected");
        sel.appendChild(opt);
      });
      wrap.appendChild(sel);
    } else {
      var value = slots[spec.key];
      var input = el("input", {
        type: spec.type,
        "data-key": spec.key,
        value: value === null || value === undefined ? "" : value,
      });
      wrap.appendChild(input);
    }
    return wrap;
  }

  function gatherFields(cardEl, intent) {
    var slots = {};
    FIELD_SPECS[intent].forEach(function (spec) {
      var fieldEl = cardEl.querySelector('[data-key="' + spec.key + '"]');
      var raw = fieldEl.value;
      if (spec.type === "number") {
        slots[spec.key] = raw === "" ? null : parseFloat(raw);
      } else if (spec.type === "patient" || spec.type === "staff" || spec.type === "followup" || spec.type === "appointment" || spec.type === "branch") {
        slots[spec.key] = raw === "" ? null : parseInt(raw, 10);
      } else {
        slots[spec.key] = raw === "" ? null : raw;
      }
    });
    return slots;
  }

  // A booking card: the phone field is "Phone (required)" while the booking has no registered patient with a
  // valid number, and a message beside it (not only the banner after Approve) says what is wrong. The rule is
  // static/booking_phone.js; `card._checkPhone(attempted)` re-reads the fields and returns the problem or null.
  function wirePhone(card, fieldsWrap, data) {
    var BP = window.BookingPhone;
    var phoneInput = fieldsWrap.querySelector('[data-key="patient_phone"]');
    var patientSel = fieldsWrap.querySelector('[data-key="patient_id"]');
    if (!BP || !phoneInput) return;
    var row = phoneInput.parentNode;
    var label = row.querySelector("label");
    var message = el("div", { class: "field-note", role: "alert", hidden: "hidden" });
    row.appendChild(message);
    var serverFlag = (data.resolved && data.resolved.phone_problem) || null;

    function check(attempted) {
      var slots = gatherFields(fieldsWrap, "book_appointment");
      var problem = BP.problem(slots, PATIENTS);
      var known = !slots.patient_id || PATIENTS.some(function (p) { return String(p.id) === String(slots.patient_id); });
      if (!problem && !known && serverFlag) problem = serverFlag;     // the page does not know this patient: the server's word
      if (label) label.textContent = (BP.required(slots, PATIENTS) || (!known && serverFlag)) ? "Phone (required)" : "Patient phone (if not registered)";
      phoneInput.setAttribute("aria-invalid", problem && attempted ? "true" : "false");
      message.textContent = problem || "";
      message.hidden = !problem;
      if (attempted) message.classList.add("field-error");
      return problem;
    }
    phoneInput.addEventListener("input", function () { check(false); });
    if (patientSel) patientSel.addEventListener("change", function () { check(false); });
    card._checkPhone = check;
    check(false);
  }

  // opts: { approveUrl, transcript, language, context, notes (array of strings),
  // onApproved(result),
  // onRejected(), buildApprovePayload(slots) -> object to POST }
  function build(data, opts) {
    var card = el("div", { class: "card" });
    if (opts.cardId) card.setAttribute("data-card-id", opts.cardId);
    card.appendChild(el("h2", { text: INTENT_LABELS[data.intent] || data.intent }));
    if (opts.transcript !== undefined) {
      card.appendChild(el("div", { class: "muted", style: "margin-bottom:10px;", text: 'Heard: "' + opts.transcript + '"' }));
    }

    // Fields the system pre-filled although the person never said them (e.g.
    // an earliest-free-slot suggestion for a WhatsApp booking request) are
    // called out, so staff don't mistake a suggestion for the patient's ask.
    if (opts.notes && opts.notes.length) {
      opts.notes.forEach(function (note) {
        card.appendChild(el("div", { class: "field-note", text: note }));
      });
    }

    var fieldsWrap = el("div", { class: "review-fields" });
    FIELD_SPECS[data.intent].forEach(function (spec) {
      fieldsWrap.appendChild(buildFieldInput(spec, data.slots, data.resolved || {}, opts.context));
    });
    card.appendChild(fieldsWrap);
    if (data.intent === "book_appointment") wirePhone(card, fieldsWrap, data);

    var actions = el("div", { class: "actions", style: "margin-top:14px;" });
    var approveBtn = el("button", { class: "btn-confirm", type: "button", text: "Approve" });
    var rejectBtn = el("button", { class: "btn-reject", type: "button", text: "Reject" });
    actions.appendChild(approveBtn);
    actions.appendChild(rejectBtn);
    card.appendChild(actions);

    function send(body) {
      return fetch(opts.approveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (r) { return r.json(); });
    }

    approveBtn.addEventListener("click", function () {
      // A new booking needs a phone number: say so beside the field and send nothing (the server checks too).
      if (card._checkPhone && card._checkPhone(true)) {
        var phoneField = card.querySelector('[data-key="patient_phone"]');
        if (phoneField && phoneField.focus) phoneField.focus();
        return;
      }
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      var slots = gatherFields(card, data.intent);
      var body = opts.buildApprovePayload ? opts.buildApprovePayload(slots) : { intent: data.intent, slots: slots };
      send(body)
        .then(function (result) {
          // The slot is inside a staff-defined booking block: nothing was
          // saved. Booking into it needs an explicit, confirmed override.
          if (!result.ok && result.blocked && window.confirm(
              String(result.error).replace(/ -- pick another slot\.?/, "") + "\n\nThe clinic blocked this time. Book anyway?")) {
            body.override_block = true;
            return send(body);
          }
          return result;
        })
        .then(function (result) {
          if (!result.ok) {
            approveBtn.disabled = false;
            rejectBtn.disabled = false;
          }
          opts.onApproved(result);
        })
        .catch(function (err) {
          approveBtn.disabled = false;
          rejectBtn.disabled = false;
          opts.onApproved({ ok: false, error: String(err) });
        });
    });

    rejectBtn.addEventListener("click", function () {
      opts.onRejected();
    });

    return card;
  }

  // Swaps a built card's editable fields + Approve/Reject buttons for a
  // plain outcome line, in place. Used so a conversation turn (Assistant
  // tab) or an inbox row (Patient messages tab) keeps showing what was
  // asked and what happened to it, instead of disappearing or forcing a
  // location.reload() to get rid of the now-stale form.
  function showOutcome(card, message, ok) {
    var fields = card.querySelector(".review-fields");
    var actions = card.querySelector(".actions");
    if (fields) fields.remove();
    if (actions) actions.remove();
    card.appendChild(el("div", { class: "flash " + (ok ? "ok" : "error"), style: "margin-top:10px;", text: message }));
  }

  // A voice edit ("make it 6 pm instead"): sets just the changed fields on the
  // card that is already on screen, leaving everything else the person typed or
  // picked alone, and briefly highlights what changed. Returns the keys it set.
  function applyChanges(card, changes) {
    var applied = [];
    Object.keys(changes || {}).forEach(function (key) {
      var field = card.querySelector('[data-key="' + key + '"]');
      if (!field) return;
      field.value = changes[key] === null || changes[key] === undefined ? "" : changes[key];
      if (field._refresh) field._refresh();
      var row = field.closest ? field.closest(".field-row") : null;
      if (row) {
        row.classList.remove("field-updated");
        void row.offsetWidth; // restart the highlight animation
        row.classList.add("field-updated");
      }
      applied.push(key);
    });
    if (card._checkPhone) card._checkPhone(false);     // a spoken phone number fills the booking's required field
    return applied;
  }

  return { build: build, showOutcome: showOutcome, setData: setData, applyChanges: applyChanges, INTENT_LABELS: INTENT_LABELS };
})();
