// Settings tab: "My branch" (this computer) and the Branches / Doctors / Weekly
// schedule editors. Every change is POSTed to a /settings/... route, which
// answers with the full refreshed branch data; the page redraws from that.
// Names and addresses are always inserted as text, never as HTML.
document.addEventListener("DOMContentLoaded", function () {
  var root = document.getElementById("settings-branches");
  var mineSelect = document.getElementById("my-branch");
  var flash = document.getElementById("settings-flash");
  if (!root || !window.Branches) return;

  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var editing = null;   // id of the branch whose row is open for editing
  var savedBranchId = null;   // a branch just saved: its row shows a check mark after the redraw
  var savedDoctorId = null;   // same for a renamed doctor

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "onclick") node.addEventListener("click", attrs[k]);
      else if (k === "onsubmit") node.addEventListener("submit", attrs[k]);
      else if (attrs[k] !== false && attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return node;
  }

  function say(message, ok) {
    if (!flash) return;
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) setTimeout(function () { flash.hidden = true; }, 4000);
    else window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function post(url, body, okMessage) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        if (!result.ok) { say(result.error || "That did not work.", false); return null; }
        if (okMessage) say(okMessage, true);
        Branches.update(result.data);
        return result;
      })
      .catch(function (err) { say("Request failed: " + err, false); return null; });
  }

  function value(id) { var el = document.getElementById(id); return el ? el.value.trim() : ""; }
  function branchName(id) { var b = Branches.get(id); return b ? b.name : "?"; }
  function doctorName(id) {
    var list = Branches.data().doctors || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i].name;
    return "?";
  }

  // ---- My branch ---------------------------------------------------------
  function drawMine() {
    if (!mineSelect) return;
    mineSelect.innerHTML = "";
    Branches.list().forEach(function (b) { mineSelect.appendChild(h("option", { value: b.id, text: b.name })); });
    mineSelect.value = String(Branches.mine());
  }
  if (mineSelect) {
    mineSelect.addEventListener("change", function () {
      Branches.setMine(parseInt(mineSelect.value, 10));
      Branches.setView(parseInt(mineSelect.value, 10));
      say("This computer now works at " + branchName(parseInt(mineSelect.value, 10)) + ".", true);
    });
  }

  // ---- Branches ----------------------------------------------------------
  function branchRow(b) {
    var isDefault = b.id === Branches.data().default_branch_id;
    if (editing === b.id) return branchEditRow(b);
    var editButton = h("button", { type: "button", class: "btn-queue", text: "Edit", onclick: function () { editing = b.id; draw(); } });
    if (savedBranchId === b.id) { savedBranchId = null; setTimeout(function () { SaveTick.show(editButton); }, 0); }
    var actions = h("td", { class: "settings-actions" }, [
      editButton,
      b.status === "open"
        ? h("button", { type: "button", class: "btn-queue btn-queue-danger", text: "Close", onclick: function () {
            var reason = window.prompt("Why is " + b.name + " closing? (shown to staff)", "Renovation");
            if (reason === null) return;
            var message = window.prompt("What should patients be told?", b.name + " is temporarily closed. We will help you book another branch.");
            if (message === null) return;
            post("/settings/branches/" + b.id + "/status", { status: "closed", reason: reason, message: message }, b.name + " is now closed.");
          } })
        : h("button", { type: "button", class: "btn-queue btn-queue-ok", text: "Reopen", onclick: function () {
            post("/settings/branches/" + b.id + "/status", { status: "open" }, b.name + " is open again.");
          } }),
      isDefault ? null : h("button", { type: "button", class: "btn-queue", text: "Make default", onclick: function () {
        post("/settings/default-branch", { branch_id: b.id }, b.name + " is the default branch.");
      } }),
      h("button", { type: "button", class: "btn-queue btn-queue-danger", text: "Remove", onclick: function () {
        if (window.confirm("Remove " + b.name + "? It must have no upcoming appointments.")) {
          post("/settings/branches/" + b.id + "/deactivate", {}, b.name + " was removed.");
        }
      } }),
    ]);
    return h("tr", {}, [
      h("td", { text: b.code }),
      h("td", { text: b.name + (isDefault ? " (default)" : "") }),
      h("td", { text: b.address || "-" }),
      h("td", { text: b.pin_code || "-" }),
      h("td", { text: b.phone || "-" }),
      h("td", {}, [h("span", { class: "state-badge state-badge-" + (b.status === "open" ? "checked_in" : "no_show"), text: b.status === "open" ? "Open" : "Closed" }),
        b.status === "closed" && b.closed_reason ? h("div", { class: "muted", text: b.closed_reason }) : null]),
      actions,
    ]);
  }

  function field(id, label, val, extra) {
    return h("label", {}, [label + " ", h("input", Object.assign({ type: "text", id: id, value: val || "" }, extra || {}))]);
  }

  function branchEditRow(b) {
    var p = "edit-branch-";
    return h("tr", { class: "settings-edit-row" }, [
      h("td", { colspan: "7" }, [
        h("div", { class: "appt-form-grid" }, [
          field(p + "code", "Code", b.code, { maxlength: "6" }),
          field(p + "name", "Name", b.name),
          field(p + "address", "Address", b.address),
          field(p + "pin", "PIN code", b.pin_code, { maxlength: "6", inputmode: "numeric" }),
          field(p + "phone", "Phone", b.phone),
          field(p + "maps", "Maps link", b.maps_url),
        ]),
        h("div", { class: "appt-form-actions" }, [
          h("button", { type: "button", class: "btn-confirm", text: "Save", onclick: function () {
            post("/settings/branches/" + b.id, {
              code: value(p + "code"), name: value(p + "name"), address: value(p + "address"),
              pin_code: value(p + "pin"), phone: value(p + "phone"), maps_url: value(p + "maps"),
            }).then(function (r) { if (r) { editing = null; savedBranchId = b.id; draw(); } });
          } }),
          h("button", { type: "button", class: "btn-queue", text: "Cancel", onclick: function () { editing = null; draw(); } }),
        ]),
      ]),
    ]);
  }

  function branchesCard() {
    var rows = Branches.list().map(branchRow);
    var table = h("table", { class: "queue-table settings-table" }, [
      h("tr", {}, ["Code", "Name", "Address", "PIN", "Phone", "Status", ""].map(function (t) { return h("th", { text: t }); })),
    ].concat(rows));
    var add = h("form", { class: "appt-form", autocomplete: "off", onsubmit: function (event) {
      event.preventDefault();
      post("/settings/branches", {
        code: value("new-branch-code"), name: value("new-branch-name"), address: value("new-branch-address"),
        pin_code: value("new-branch-pin"), phone: value("new-branch-phone"),
      }, "Branch added. Give it a doctor schedule below so it can take bookings.");
    } }, [
      h("div", { class: "mini-form-title", text: "Add a branch" }),
      h("div", { class: "appt-form-grid" }, [
        field("new-branch-code", "Code", "", { maxlength: "6", placeholder: "D", required: "required" }),
        field("new-branch-name", "Name", "", { placeholder: "Branch D", required: "required" }),
        field("new-branch-address", "Address", ""),
        field("new-branch-pin", "PIN code", "", { maxlength: "6", inputmode: "numeric", placeholder: "122001" }),
        field("new-branch-phone", "Phone", ""),
      ]),
      h("div", { class: "appt-form-actions" }, [h("button", { type: "submit", class: "btn-confirm", text: "Add branch" })]),
    ]);
    return h("div", { class: "card" }, [
      h("h2", { text: "Branches" }),
      h("p", { class: "muted", text: "The PIN code is how the patient's nearest branch is worked out on WhatsApp (branches sharing more leading digits with the patient's PIN come first)." }),
      table, add,
    ]);
  }

  // ---- Doctors -----------------------------------------------------------
  function doctorsCard() {
    var doctors = Branches.data().doctors || [];
    var table = h("table", { class: "queue-table settings-table" }, [
      h("tr", {}, ["Name", "Title", "Specialty", ""].map(function (t) { return h("th", { text: t }); })),
    ].concat(doctors.map(function (d) {
      var renameButton = h("button", { type: "button", class: "btn-queue", text: "Rename", onclick: function () {
        var name = window.prompt("Doctor's name", d.name);
        if (!name) return;
        savedDoctorId = d.id;     // the table is redrawn by the save itself, so mark it first
        post("/settings/doctors/" + d.id, { name: name }).then(function (r) { if (!r) { savedDoctorId = null; } });
      } });
      if (savedDoctorId === d.id) { savedDoctorId = null; setTimeout(function () { SaveTick.show(renameButton); }, 0); }
      return h("tr", {}, [
        h("td", { text: d.name }), h("td", { text: d.title || "-" }), h("td", { text: d.specialty || "-" }),
        h("td", { class: "settings-actions" }, [renameButton]),
      ]);
    })));
    var add = h("form", { class: "appt-form", autocomplete: "off", onsubmit: function (event) {
      event.preventDefault();
      post("/settings/doctors", { name: value("new-doctor-name"), title: value("new-doctor-title"), specialty: value("new-doctor-specialty") }, "Doctor added.");
    } }, [
      h("div", { class: "mini-form-title", text: "Add a doctor" }),
      h("div", { class: "appt-form-grid" }, [
        field("new-doctor-name", "Name", "", { placeholder: "Dr. Sharma", required: "required" }),
        field("new-doctor-title", "Title", "", { placeholder: "Dr." }),
        field("new-doctor-specialty", "Specialty", "", { placeholder: "General physician" }),
      ]),
      h("div", { class: "appt-form-actions" }, [h("button", { type: "submit", class: "btn-confirm", text: "Add doctor" })]),
    ]);
    return h("div", { class: "card" }, [h("h2", { text: "Doctors" }), table, add]);
  }

  // ---- Weekly schedule ---------------------------------------------------
  function scheduleCard() {
    var schedules = Branches.data().schedules || [];
    var rows = schedules.map(function (s) {
      return h("tr", {}, [
        h("td", { text: s.branch_name }), h("td", { text: DAYS[s.weekday] }),
        h("td", { text: s.start_time + "-" + s.end_time }), h("td", { text: s.doctor_name }),
        h("td", { class: "muted", text: s.valid_from || s.valid_to ? (s.valid_from || "...") + " to " + (s.valid_to || "...") : "" }),
        h("td", {}, [h("button", { type: "button", class: "btn-queue btn-queue-danger", text: "Remove", onclick: function () {
          post("/settings/schedules/" + s.id + "/remove", {}, "Removed.");
        } })]),
      ]);
    });
    var table = h("table", { class: "queue-table settings-table" }, [
      h("tr", {}, ["Branch", "Day", "Hours", "Doctor", "Dates", ""].map(function (t) { return h("th", { text: t }); })),
    ].concat(rows));

    var doctorSelect = h("select", { id: "sched-doctor" }, (Branches.data().doctors || []).map(function (d) { return h("option", { value: d.id, text: d.name }); }));
    var branchSelect = h("select", { id: "sched-branch" }, Branches.list().map(function (b) { return h("option", { value: b.id, text: b.name }); }));
    var days = h("div", { class: "weekday-picks" }, DAYS.map(function (d, i) {
      return h("label", { class: "weekday-pick" }, [h("input", { type: "checkbox", value: i, "data-weekday": "1", checked: i < 6 ? "checked" : false }), " " + d]);
    }));
    var add = h("form", { class: "appt-form", autocomplete: "off", onsubmit: function (event) {
      event.preventDefault();
      var picked = Array.prototype.map.call(root.querySelectorAll("input[data-weekday]:checked"), function (el) { return parseInt(el.value, 10); });
      if (!picked.length) { say("Tick at least one weekday.", false); return; }
      post("/settings/schedules", {
        doctor_id: parseInt(doctorSelect.value, 10), branch_id: parseInt(branchSelect.value, 10), weekdays: picked,
        start_time: value("sched-start"), end_time: value("sched-end"), valid_from: value("sched-from"), valid_to: value("sched-to"),
      }, "Schedule added.");
    } }, [
      h("div", { class: "mini-form-title", text: "Put a doctor at a branch" }),
      h("p", { class: "muted", text: "One doctor per branch at a time, and a doctor cannot be at two branches at once. A branch can only take bookings at hours when one of its doctors is scheduled. Add morning and evening windows separately." }),
      h("div", { class: "appt-form-grid" }, [
        h("label", {}, ["Doctor ", doctorSelect]), h("label", {}, ["Branch ", branchSelect]),
        h("label", {}, ["From ", h("input", { type: "time", id: "sched-start", value: "09:00" })]),
        h("label", {}, ["To ", h("input", { type: "time", id: "sched-end", value: "13:00" })]),
        h("label", {}, ["Only from (optional) ", h("input", { type: "date", id: "sched-from" })]),
        h("label", {}, ["Only until (optional) ", h("input", { type: "date", id: "sched-to" })]),
      ]),
      days,
      h("div", { class: "appt-form-actions" }, [h("button", { type: "submit", class: "btn-confirm", text: "Add schedule" })]),
    ]);
    return h("div", { class: "card" }, [h("h2", { text: "Doctor schedules" }), table, add]);
  }

  function draw() {
    drawMine();
    root.innerHTML = "";
    root.appendChild(branchesCard());
    root.appendChild(doctorsCard());
    root.appendChild(scheduleCard());
  }

  document.addEventListener("branchdata", draw);
  document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "settings") draw(); });
  draw();
});
