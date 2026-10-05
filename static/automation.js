// Automation tab: the on/off switch, the daily cap, booking blocks, and the
// automated-actions feed (with Undo) -- plus the patient appointment timeline
// shown when a patient is clicked in the Patients tab.
//
// All data is rendered with textContent (names and reasons come from
// patients), never innerHTML.
document.addEventListener("DOMContentLoaded", function () {
  var section = document.querySelector('.tab-panel[data-tab="automation"]');
  // An app started before this feature existed renders only a "restart the
  // app" notice in the tab; there is nothing to wire up then.
  if (!section || !document.getElementById("auto-switch")) return;

  var flash = document.getElementById("automation-flash");
  var switchBtn = document.getElementById("auto-switch");
  var switchLabel = document.getElementById("auto-switch-label");
  var capInput = document.getElementById("auto-cap");
  var capSave = document.getElementById("auto-cap-save");
  var capUsed = document.getElementById("auto-cap-used");
  var blockForm = document.getElementById("block-form");
  var blockWarning = document.getElementById("block-warning");
  var blocksList = document.getElementById("blocks-list");
  var feedList = document.getElementById("feed-list");
  var feedCount = document.getElementById("feed-count");
  var feedSearch = document.getElementById("feed-search");
  var REFRESH_MS = 15000;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function showFlash(message, ok) {
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) setTimeout(function () { flash.hidden = true; }, 5000);
  }

  function api(url, body) {
    var options = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    return fetch(url, options).then(function (r) { return r.json(); });
  }

  // --- settings -----------------------------------------------------------
  function renderSettings(s) {
    switchBtn.classList.toggle("is-on", !!s.enabled);
    switchBtn.setAttribute("aria-checked", s.enabled ? "true" : "false");
    switchLabel.textContent = s.enabled
      ? "Automatic appointments are ON"
      : "Automatic appointments are OFF -- every request waits for your approval";
    if (document.activeElement !== capInput) capInput.value = s.daily_cap;
    capUsed.textContent = s.used_today + " of " + s.daily_cap + " used today";
  }

  switchBtn.addEventListener("click", function () {
    var turnOn = !switchBtn.classList.contains("is-on");
    if (!turnOn && !window.confirm("Turn automatic appointments OFF? Every patient request will wait for your approval, as before.")) return;
    switchBtn.disabled = true;
    api("/automation/settings", { enabled: turnOn })
      .then(function (r) {
        if (!r.ok) throw new Error(r.error || "failed");
        renderSettings(r.settings);
        showFlash(turnOn ? "Automatic appointments are ON." : "Automatic appointments are OFF.", true);
      })
      .catch(function (err) { showFlash("Could not change the switch: " + err.message, false); })
      .then(function () { switchBtn.disabled = false; });
  });

  capSave.addEventListener("click", function () {
    var value = parseInt(capInput.value, 10);
    if (isNaN(value)) { showFlash("Enter a whole number for the cap.", false); return; }
    api("/automation/settings", { daily_cap: value })
      .then(function (r) {
        if (!r.ok) throw new Error(r.error || "failed");
        renderSettings(r.settings);
        showFlash("Daily cap saved: " + r.settings.daily_cap + ".", true);
      })
      .catch(function (err) { showFlash(err.message, false); });
  });

  // --- blocks -------------------------------------------------------------
  function describeBlock(b) {
    var days = b.start_date === b.end_date ? b.start_date : b.start_date + " to " + b.end_date;
    var when = b.start_time ? days + ", " + b.start_time + "-" + b.end_time : days + " (whole day)";
    if (b.branch) when += " at " + b.branch;
    if (b.doctor) when += " for " + b.doctor;
    return when;
  }

  function affectedList(affected) {
    var list = el("ul", { class: "block-affected" });
    affected.forEach(function (a) {
      list.appendChild(el("li", { text: a.appt_date + " " + a.start_time + " -- " + (a.name || "(no name)") + (a.phone ? " (" + a.phone + ")" : "") }));
    });
    return list;
  }

  function renderBlocks(blocks) {
    blocksList.innerHTML = "";
    if (!blocks.length) {
      blocksList.appendChild(el("p", { class: "muted", text: "No blocks." }));
      return;
    }
    blocks.forEach(function (b) {
      var row = el("div", { class: "block-row" });
      var head = el("div", { class: "block-head" });
      head.appendChild(el("strong", { text: describeBlock(b) }));
      if (b.reason) head.appendChild(el("span", { class: "muted", text: " -- " + b.reason }));
      var remove = el("button", { type: "button", class: "btn-queue btn-queue-danger", text: "Remove" });
      remove.addEventListener("click", function () {
        if (!window.confirm("Remove this block? New appointments can be made in that window again.")) return;
        remove.disabled = true;
        api("/automation/blocks/" + b.id + "/remove", {})
          .then(function (r) { if (!r.ok) throw new Error(r.error || "failed"); showFlash("Block removed.", true); return load(); })
          .catch(function (err) { showFlash(err.message, false); remove.disabled = false; });
      });
      head.appendChild(remove);
      row.appendChild(head);
      if (b.affected && b.affected.length) {
        var details = el("details", { class: "block-existing" });
        details.appendChild(el("summary", { text: b.affected.length + " existing appointment" + (b.affected.length === 1 ? "" : "s") + " inside this block (not changed -- handle them in the Queue tab)" }));
        details.appendChild(affectedList(b.affected));
        row.appendChild(details);
      }
      blocksList.appendChild(row);
    });
  }

  function blockBody() {
    return {
      start_date: document.getElementById("block-start-date").value,
      end_date: document.getElementById("block-end-date").value || document.getElementById("block-start-date").value,
      start_time: document.getElementById("block-start-time").value,
      end_time: document.getElementById("block-end-time").value,
      reason: document.getElementById("block-reason").value,
      branch_id: blockBranch && blockBranch.value ? parseInt(blockBranch.value, 10) : null,
      doctor_id: blockDoctor && blockDoctor.value ? parseInt(blockDoctor.value, 10) : null,
    };
  }

  // Who a block applies to: every branch / doctor, or one of them.
  var blockBranch = document.getElementById("block-branch");
  var blockDoctor = document.getElementById("block-doctor");
  function fillBlockScope() {
    if (!window.Branches || !blockBranch || !blockDoctor) return;
    [blockBranch, blockDoctor].forEach(function (select) { var l = select.closest(".branch-field"); if (l) l.hidden = !Branches.multi(); });
    var chosenBranch = blockBranch.value, chosenDoctor = blockDoctor.value;
    blockBranch.innerHTML = "";
    blockBranch.appendChild(el("option", { value: "", text: "All branches" }));
    Branches.list().forEach(function (b) { blockBranch.appendChild(el("option", { value: b.id, text: b.name })); });
    blockDoctor.innerHTML = "";
    blockDoctor.appendChild(el("option", { value: "", text: "All doctors" }));
    (Branches.data().doctors || []).forEach(function (d) { blockDoctor.appendChild(el("option", { value: d.id, text: d.name })); });
    blockBranch.value = chosenBranch; blockDoctor.value = chosenDoctor;
  }
  document.addEventListener("branchdata", fillBlockScope);
  document.addEventListener("DOMContentLoaded", fillBlockScope);
  fillBlockScope();

  var pendingBlock = null;   // a block waiting for "Add anyway" after the existing-appointments warning

  function createBlock(body) {
    return api("/automation/blocks", body).then(function (r) {
      if (!r.ok) throw new Error(r.error || "failed");
      blockWarning.hidden = true;
      pendingBlock = null;
      blockForm.reset();
      showFlash("Block added" + (r.count ? " -- " + r.count + " existing appointment" + (r.count === 1 ? "" : "s") + " fall inside it and were not changed." : "."), true);
      return load();
    });
  }

  blockForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var body = blockBody();
    api("/automation/blocks/preview", body)
      .then(function (r) {
        if (!r.ok) throw new Error(r.error || "failed");
        if (r.count === 0) return createBlock(body);
        // Warn first: list who is already booked in that window.
        blockWarning.hidden = false;
        blockWarning.innerHTML = "";
        blockWarning.appendChild(el("div", { text: r.count + " appointment" + (r.count === 1 ? " is" : "s are") + " already booked in this window. Adding the block will NOT move or cancel them -- you will need to handle each one:" }));
        blockWarning.appendChild(affectedList(r.affected));
        var go = el("button", { type: "button", class: "btn-confirm", text: "Add block anyway" });
        go.addEventListener("click", function () {
          go.disabled = true;
          createBlock(body).catch(function (err) { showFlash(err.message, false); go.disabled = false; });
        });
        var back = el("button", { type: "button", class: "btn-queue", text: "Cancel" });
        back.addEventListener("click", function () { blockWarning.hidden = true; });
        blockWarning.appendChild(el("div", { class: "appt-form-actions" }, [go, back]));
      })
      .catch(function (err) { showFlash(err.message, false); });
  });

  // --- feed ---------------------------------------------------------------
  function slotText(item) {
    var m = item.meta || {};
    if (item.event === "auto_rescheduled" && m.old_date) return m.old_date + " " + m.old_time + " -> " + m.appt_date + " " + m.start_time;
    if (item.event === "auto_cancelled" && m.old_date) return m.old_date + " " + m.old_time;
    if (m.appt_date) return m.appt_date + (m.start_time ? " " + m.start_time : "");
    return "";
  }

  var RESULT = {
    auto_booked: ["Done", "ok"], auto_cancelled: ["Done", "ok"], auto_rescheduled: ["Done", "ok"],
    escalated: ["Sent to staff", "warn"], blocked: ["Offered other times", "info"],
    conflict: ["Offered other times", "info"], undone: ["Undone", "muted"],
  };

  function renderFeed(feed) {
    feedList.innerHTML = "";
    feedCount.textContent = feed.length ? "(" + feed.length + ")" : "";
    if (!feed.length) {
      feedList.appendChild(el("p", { class: "muted", text: feedSearch.value ? "Nothing matches that filter." : "No automated actions yet." }));
      return;
    }
    var table = el("table", { class: "feed-table" });
    table.appendChild(el("tr", {}, ["When", "Patient", "Action", "Slot", "Result", "Details", ""].map(function (h) { return el("th", { text: h }); })));
    feed.forEach(function (item) {
      var tr = el("tr", { class: item.undone ? "feed-undone" : "" });
      tr.appendChild(el("td", { class: "muted feed-when", text: item.created_at }));
      var who = el("td", {}, [el("div", { text: item.patient_name || "(unknown)" })]);
      if (item.wa_id) who.appendChild(el("div", { class: "muted queue-phone", text: item.wa_id }));
      tr.appendChild(who);
      tr.appendChild(el("td", { text: item.label }));
      tr.appendChild(el("td", { text: slotText(item) }));
      var res = RESULT[item.event] || ["", "muted"];
      var resultCell = el("td", {}, [el("span", { class: "feed-badge feed-badge-" + res[1], text: item.undone ? "Undone" : res[0] })]);
      tr.appendChild(resultCell);
      var detail = item.detail || "";
      if (item.event === "escalated" && item.meta && item.meta.reason) detail = "Needs staff: " + item.meta.reason;
      tr.appendChild(el("td", { class: "feed-detail", text: detail }));
      var action = el("td", {});
      if (item.undo_available) {
        var undo = el("button", { type: "button", class: "btn-queue btn-queue-danger", text: "Undo" });
        undo.addEventListener("click", function () {
          if (!window.confirm("Undo this action?\n\n" + item.label + " -- " + (item.patient_name || "patient") + ". The patient will be told on WhatsApp.")) return;
          undo.disabled = true;
          api("/automation/undo/" + item.id, {})
            .then(function (r) { showFlash(r.ok ? r.message : r.error, !!r.ok); return load(); })
            .catch(function (err) { showFlash("Request failed: " + err, false); undo.disabled = false; });
        });
        action.appendChild(undo);
      } else if (item.undo_blocked_reason && item.undo_blocked_reason !== "already undone") {
        action.appendChild(el("span", { class: "muted feed-nounto", title: item.undo_blocked_reason, text: "can't undo" }));
      }
      tr.appendChild(action);
      table.appendChild(tr);
    });
    feedList.appendChild(table);
  }

  // --- load ---------------------------------------------------------------
  var loading = false;
  function load() {
    if (loading) return Promise.resolve();
    loading = true;
    return api("/automation/data?q=" + encodeURIComponent(feedSearch.value || ""))
      .then(function (data) {
        if (!data.ok) return;
        renderSettings(data.settings);
        renderBlocks(data.blocks);
        renderFeed(data.feed);
      })
      .catch(function () { /* keep the last good view */ })
      .then(function () { loading = false; });
  }

  var searchTimer = null;
  feedSearch.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(load, 250);
  });

  document.addEventListener("tabchange", function (event) {
    if (event.detail && event.detail.id === "automation") load();
  });
  setInterval(function () {
    if (!section.hidden && document.visibilityState === "visible") load();
  }, REFRESH_MS);

  // --- patient appointment timeline (Patients tab) -------------------------
  var patientsCard = document.getElementById("patients-table-card");
  var timelineCard = document.getElementById("patient-activity-card");
  var timelineTitle = document.getElementById("patient-activity-title");
  var timelineList = document.getElementById("patient-activity-list");

  function showTimeline(patientId) {
    timelineCard.hidden = false;
    timelineTitle.textContent = "Appointment activity";
    timelineList.innerHTML = "";
    timelineList.appendChild(el("p", { class: "muted", text: "Loading..." }));
    fetch("/patients/" + patientId + "/activity")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "failed");
        timelineTitle.textContent = "Appointment activity -- " + data.patient.name;
        timelineList.innerHTML = "";
        if (!data.activity.length) {
          timelineList.appendChild(el("p", { class: "muted", text: "No appointment activity recorded for this patient yet." }));
          return;
        }
        var table = el("table", { class: "feed-table" });
        table.appendChild(el("tr", {}, ["When", "Event", "Source", "Details"].map(function (h) { return el("th", { text: h }); })));
        data.activity.forEach(function (a) {
          var tr = el("tr", { class: a.undone ? "feed-undone" : "" });
          tr.appendChild(el("td", { class: "muted feed-when", text: a.created_at }));
          tr.appendChild(el("td", { text: a.label + (a.undone ? " (undone)" : "") }));
          tr.appendChild(el("td", { class: "muted", text: a.source === "staff" ? "Staff" : "WhatsApp assistant" }));
          tr.appendChild(el("td", { class: "feed-detail", text: a.detail || "" }));
          table.appendChild(tr);
        });
        timelineList.appendChild(table);
      })
      .catch(function (err) {
        timelineList.innerHTML = "";
        timelineList.appendChild(el("p", { class: "muted", text: "Could not load the activity: " + err.message }));
      });
  }

  if (patientsCard && timelineCard) {
    patientsCard.addEventListener("click", function (event) {
      var row = event.target.closest("tr[data-patient-id]");
      if (row) showTimeline(row.getAttribute("data-patient-id"));
    });
    patientsCard.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      var row = event.target.closest("tr[data-patient-id]");
      if (row) showTimeline(row.getAttribute("data-patient-id"));
    });
  }
});
