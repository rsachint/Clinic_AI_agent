// Queue tab, staff direct edits: the date picker, the "New appointment" form
// and the per-row Move / Cancel buttons.
//
// The click or submit IS the explicit human action, so these POST to endpoints
// that book / move / cancel straight away (audited on the server, patient
// notified automatically) -- no review card. Slots offered here come from
// /appointments/slots, which already leaves out booked and blocked times; a
// blocked time is shown separately and needs a "Book anyway?" confirmation.
document.addEventListener("DOMContentLoaded", function () {
  var panel = document.getElementById("queue-panel");
  var flash = document.getElementById("queue-flash");
  var dateInput = document.getElementById("queue-date");
  if (!panel || !dateInput) return;

  var todayBtn = document.getElementById("queue-date-today");
  var toggleBtn = document.getElementById("new-appt-toggle");
  var form = document.getElementById("new-appt-form");
  var patientSel = document.getElementById("new-appt-patient");
  var nameInput = document.getElementById("new-appt-name");
  var phoneInput = document.getElementById("new-appt-phone");
  var apptDate = document.getElementById("new-appt-date");
  var apptTime = document.getElementById("new-appt-time");
  var hint = document.getElementById("new-appt-hint");
  var closeBtn = document.getElementById("new-appt-cancel");
  var submitBtn = document.getElementById("new-appt-submit");

  var movePanel = document.getElementById("move-appt");
  var moveTitle = document.getElementById("move-appt-title");
  var moveDate = document.getElementById("move-appt-date");
  var moveTime = document.getElementById("move-appt-time");
  var moveHint = document.getElementById("move-appt-hint");
  var moveSubmit = document.getElementById("move-appt-submit");
  var moveClose = document.getElementById("move-appt-cancel");
  var moveId = null;

  var todayIso = dateInput.value;
  var newBranch = document.getElementById("new-appt-branch");
  var moveBranch = document.getElementById("move-appt-branch");

  // Fill a branch <select> (shown only when there is more than one branch).
  function fillBranches(select, chosenId) {
    if (!select || !window.Branches) return;
    var label = select.closest(".branch-field");
    if (label) label.hidden = !Branches.multi();
    select.innerHTML = "";
    Branches.list().forEach(function (b) {
      var o = document.createElement("option");
      o.value = b.id;
      o.textContent = b.name + (b.status === "closed" ? " (closed)" : "");
      select.appendChild(o);
    });
    select.value = String(chosenId || Branches.bookingBranch());
  }
  function branchValue(select) { return select && select.value ? select.value : (window.Branches ? Branches.bookingBranch() : ""); }

  function showFlash(message, ok) {
    if (!flash) return;
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) setTimeout(function () { flash.hidden = true; }, 5000);
    else window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function refreshQueue() {
    return window.DashboardRefresh ? DashboardRefresh.refreshQueue() : Promise.resolve();
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json(); });
  }

  function option(value, text, disabled) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    if (disabled) o.disabled = true;
    return o;
  }

  // Fill a <select> with a date's free slots (plus, separately, the ones a
  // booking block is holding back). Calls back with the slot lists.
  function loadSlots(select, dateValue, hintEl, done, branchId) {
    select.innerHTML = "";
    if (!dateValue) {
      select.appendChild(option("", "Pick a date first", true));
      return;
    }
    select.appendChild(option("", "Loading...", true));
    fetch("/appointments/slots?date=" + encodeURIComponent(dateValue) +
          (branchId ? "&branch=" + encodeURIComponent(branchId) : ""))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        select.innerHTML = "";
        if (!data.ok) {
          select.appendChild(option("", data.error || "Could not load times", true));
          return;
        }
        if (!data.free.length && !data.blocked.length) {
          select.appendChild(option("", "No free times that day", true));
        } else {
          select.appendChild(option("", "Pick a time", false));
        }
        data.free.forEach(function (t) { select.appendChild(option(t, t)); });
        if (data.blocked.length) {
          var group = document.createElement("optgroup");
          group.label = "Blocked -- needs confirmation";
          data.blocked.forEach(function (t) { group.appendChild(option(t, t + " (blocked)")); });
          select.appendChild(group);
        }
        if (hintEl) {
          hintEl.textContent = data.free.length + " free time" + (data.free.length === 1 ? "" : "s") +
            (data.doctor ? " with " + data.doctor : "") + (!data.free.length && data.hours === "closed" ? " (branch closed that day)" : "");
        }
        if (done) done(data);
      })
      .catch(function () {
        select.innerHTML = "";
        select.appendChild(option("", "Could not load times", true));
      });
  }

  function isBlockedChoice(select) {
    var chosen = select.options[select.selectedIndex];
    return !!(chosen && chosen.parentNode && chosen.parentNode.tagName === "OPTGROUP");
  }

  // --- date picker -------------------------------------------------------
  dateInput.addEventListener("change", function () { refreshQueue(); });
  if (todayBtn) todayBtn.addEventListener("click", function () { dateInput.value = todayIso; refreshQueue(); });

  // --- New appointment ---------------------------------------------------
  function syncUnregistered() {
    var registered = !!patientSel.value;
    document.querySelectorAll(".new-appt-unreg").forEach(function (el) { el.hidden = registered; });
  }
  patientSel.addEventListener("change", syncUnregistered);
  syncUnregistered();

  function openForm(open) {
    form.hidden = !open;
    toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      fillBranches(newBranch);
      apptDate.value = dateInput.value >= todayIso ? dateInput.value : todayIso;
      loadSlots(apptTime, apptDate.value, hint, null, branchValue(newBranch));
    }
  }
  toggleBtn.addEventListener("click", function () { openForm(form.hidden); });
  closeBtn.addEventListener("click", function () { openForm(false); });
  apptDate.addEventListener("change", function () { loadSlots(apptTime, apptDate.value, hint, null, branchValue(newBranch)); });
  if (newBranch) newBranch.addEventListener("change", function () { loadSlots(apptTime, apptDate.value, hint, null, branchValue(newBranch)); });

  function submitNew(override) {
    var body = {
      patient_id: patientSel.value ? parseInt(patientSel.value, 10) : null,
      patient_name: nameInput.value,
      patient_phone: phoneInput.value,
      appt_date: apptDate.value,
      start_time: apptTime.value,
      branch_id: branchValue(newBranch) ? parseInt(branchValue(newBranch), 10) : null,
      override_block: !!override,
    };
    submitBtn.disabled = true;
    return postJson("/appointments/new", body)
      .then(function (result) {
        if (result.ok) {
          showFlash(result.message, true);
          nameInput.value = "";
          phoneInput.value = "";
          patientSel.value = "";
          syncUnregistered();
          loadSlots(apptTime, apptDate.value, hint, null, branchValue(newBranch));
          if (apptDate.value) dateInput.value = apptDate.value;
          return refreshQueue();
        }
        if (result.blocked && !override) {
          if (window.confirm(result.error.replace(/ -- pick another slot\.?/, "") + "\n\nThe clinic blocked this time. Book anyway?")) {
            return submitNew(true);
          }
          return null;
        }
        showFlash(result.error || "That did not work.", false);
        return null;
      })
      .catch(function (err) { showFlash("Request failed: " + err, false); })
      .then(function () { submitBtn.disabled = false; });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!apptTime.value) { showFlash("Pick a time from the list.", false); return; }
    // A time from the "Blocked" group is a deliberate override: ask first.
    if (isBlockedChoice(apptTime)) {
      if (!window.confirm("The clinic blocked " + apptTime.value + " on " + apptDate.value + ". Book anyway?")) return;
      submitNew(true);
    } else {
      submitNew(false);
    }
  });

  document.addEventListener("branchchange", function () {
    refreshQueue();
    if (!form.hidden) { fillBranches(newBranch); loadSlots(apptTime, apptDate.value, hint, null, branchValue(newBranch)); }
  });
  document.addEventListener("branchdata", function () { refreshQueue(); });

  // --- Move / Cancel buttons on queue rows -------------------------------
  function openMove(btn) {
    moveId = btn.getAttribute("data-appointment-id");
    var name = btn.getAttribute("data-name") || "this patient";
    moveTitle.textContent = "Move " + name + " (now " + btn.getAttribute("data-date") + " " + btn.getAttribute("data-time") + ")";
    moveDate.value = btn.getAttribute("data-date");
    moveHint.textContent = "";
    movePanel.hidden = false;
    fillBranches(moveBranch, btn.getAttribute("data-branch"));
    loadSlots(moveTime, moveDate.value, moveHint, null, branchValue(moveBranch));
    movePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  moveDate.addEventListener("change", function () { loadSlots(moveTime, moveDate.value, moveHint, null, branchValue(moveBranch)); });
  if (moveBranch) moveBranch.addEventListener("change", function () { loadSlots(moveTime, moveDate.value, moveHint, null, branchValue(moveBranch)); });
  moveClose.addEventListener("click", function () { movePanel.hidden = true; moveId = null; });

  function submitMove(override) {
    moveSubmit.disabled = true;
    return postJson("/appointments/" + moveId + "/move", {
      appt_date: moveDate.value, start_time: moveTime.value, override_block: !!override,
      branch_id: moveBranch && moveBranch.value ? parseInt(moveBranch.value, 10) : null,
    })
      .then(function (result) {
        if (result.ok) {
          showFlash(result.message, true);
          movePanel.hidden = true;
          moveId = null;
          return refreshQueue();
        }
        if (result.blocked && !override) {
          if (window.confirm(result.error.replace(/ -- pick another slot\.?/, "") + "\n\nThe clinic blocked this time. Move anyway?")) {
            return submitMove(true);
          }
          return null;
        }
        showFlash(result.error || "That did not work.", false);
        return null;
      })
      .catch(function (err) { showFlash("Request failed: " + err, false); })
      .then(function () { moveSubmit.disabled = false; });
  }
  moveSubmit.addEventListener("click", function () {
    if (!moveId) return;
    if (!moveTime.value) { showFlash("Pick a time from the list.", false); return; }
    if (isBlockedChoice(moveTime)) {
      if (!window.confirm("The clinic blocked " + moveTime.value + " on " + moveDate.value + ". Move anyway?")) return;
      submitMove(true);
    } else {
      submitMove(false);
    }
  });

  panel.addEventListener("click", function (event) {
    var btn = event.target.closest("button[data-queue-edit]");
    if (!btn || btn.disabled) return;
    var kind = btn.getAttribute("data-queue-edit");
    if (kind === "move") {
      openMove(btn);
      return;
    }
    var name = btn.getAttribute("data-name") || "this patient";
    var when = btn.getAttribute("data-date") + " at " + btn.getAttribute("data-time");
    if (!window.confirm("Cancel " + name + "'s appointment on " + when + "?\n\nThe patient will be told on WhatsApp.")) return;
    btn.disabled = true;
    postJson("/appointments/" + btn.getAttribute("data-appointment-id") + "/cancel", {})
      .then(function (result) { showFlash(result.ok ? result.message : (result.error || "That did not work."), !!result.ok); })
      .catch(function (err) { showFlash("Request failed: " + err, false); })
      .then(refreshQueue);
  });
});
